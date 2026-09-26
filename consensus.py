"""
consensus.py
BLCH9X2 Assignment 7: Consensus and Forks Lab

A small, self-contained Nakamoto-consensus simulator.

What it provides
  * Tx / Block with real (low-difficulty) double-SHA-256 proof of work and a
    UTXO-lite ledger, so forks can reverse real payments.
  * Node: keeps every valid block it has seen (a block tree, not a single list),
    an orphan pool for blocks whose parent has not arrived yet, a mempool, and a
    fork-choice rule ("length" = longest chain, the primary rule in this lab;
    "work" = heaviest cumulative work, kept as an extension) with a configurable
    tie-break ("first_seen", "lowest_hash", "random").
  * Network: deterministic discrete-event simulation of gossip between nodes with
    per-link latency, optional bandwidth (large blocks arrive late), partitions
    and message withholding.
  * Scenarios for the brief: fork + resolution, ties, delayed / out-of-order
    arrival, longest-chain vs heaviest-work, and a R10 000 double-spend.
  * Analytics: Nakamoto (2008) and Rosenfeld (2014) attacker-success formulas,
    a Monte Carlo check, and a stale-rate simulator used for Part B.

Usage
  python consensus.py all            run every scenario and write results/*.json
  python consensus.py fork | tie | delay | heaviest | doublespend | depth
"""
from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

BLOCK_REWARD = 50          # coinbase units (not rand)
DEFAULT_BITS = 12          # leading zero bits; ~4 096 hashes per block on average
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def sha256d(data: bytes) -> str:
    return hashlib.sha256(hashlib.sha256(data).digest()).hexdigest()


@dataclass(frozen=True)
class Tx:
    """UTXO-lite transaction. inputs are coin ids "<txid>:<index>"."""
    inputs: Tuple[str, ...]
    outputs: Tuple[Tuple[str, int], ...]      # (owner, amount)
    memo: str = ""

    @property
    def txid(self) -> str:
        body = json.dumps([list(self.inputs), [list(o) for o in self.outputs], self.memo])
        return sha256d(body.encode())[:16]

    @property
    def is_coinbase(self) -> bool:
        return len(self.inputs) == 0

    def size(self) -> int:
        return 60 + 40 * len(self.inputs) + 34 * len(self.outputs)


def merkle_root(txids: List[str]) -> str:
    layer = list(txids) or [""]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256d((layer[i] + layer[i + 1]).encode()) for i in range(0, len(layer), 2)]
    return sha256d(layer[0].encode())


@dataclass
class Block:
    height: int
    prev_hash: str
    miner: str
    txs: Tuple[Tx, ...]
    bits: int
    timestamp: float
    nonce: int = 0
    payload_bytes: int = 0          # padding to simulate a large block on the wire
    hash: str = ""

    def header(self) -> bytes:
        return json.dumps([self.height, self.prev_hash, merkle_root([t.txid for t in self.txs]),
                           self.bits, round(self.timestamp, 6), self.miner, self.nonce]).encode()

    def compute_hash(self) -> str:
        return sha256d(self.header())

    @property
    def target(self) -> int:
        return 2 ** (256 - self.bits)

    @property
    def work(self) -> int:
        """Expected number of hashes to find this block (2**bits)."""
        return 2 ** self.bits

    def pow_ok(self) -> bool:
        return self.hash == self.compute_hash() and int(self.hash, 16) < self.target

    def size(self) -> int:
        return 80 + sum(t.size() for t in self.txs) + self.payload_bytes


def make_genesis(allocations: Optional[Dict[str, int]] = None) -> Block:
    allocations = allocations or {"attacker": 10_000, "alice": 5_000, "bob": 5_000}
    tx = Tx((), tuple(sorted(allocations.items())), "genesis")
    g = Block(0, "0" * 64, "genesis", (tx,), bits=0, timestamp=0.0)
    g.hash = g.compute_hash()
    return g


def mine(prev: Block, miner: str, txs: Iterable[Tx], bits: int, timestamp: float,
         payload_bytes: int = 0, max_tries: int = 50_000_000) -> Block:
    height = prev.height + 1
    coinbase = Tx((), ((miner, BLOCK_REWARD),), f"coinbase:{height}:{miner}:{prev.hash[:12]}")
    b = Block(height, prev.hash, miner, (coinbase, *txs), bits, timestamp, 0, payload_bytes)
    target = b.target
    for nonce in range(max_tries):
        b.nonce = nonce
        h = b.compute_hash()
        if int(h, 16) < target:
            b.hash = h
            return b
    raise RuntimeError("mining failed; lower bits")


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
class InvalidBlock(Exception):
    pass


def apply_block(utxo: Dict[str, Tuple[str, int]], block: Block) -> Dict[str, Tuple[str, int]]:
    """Return the UTXO set after block, or raise InvalidBlock."""
    new = dict(utxo)
    fees = 0
    for i, tx in enumerate(block.txs):
        if tx.is_coinbase:
            if i != 0 and block.height != 0:
                raise InvalidBlock("coinbase not first")
            continue
        total_in = 0
        for cid in tx.inputs:
            if cid not in new:
                raise InvalidBlock(f"tx {tx.txid} spends missing/spent coin {cid}")
            total_in += new.pop(cid)[1]
        total_out = sum(a for _, a in tx.outputs)
        if total_out > total_in:
            raise InvalidBlock(f"tx {tx.txid} creates value")
        fees += total_in - total_out
        for j, out in enumerate(tx.outputs):
            new[f"{tx.txid}:{j}"] = out
    cb = block.txs[0]
    if block.height > 0:
        if not cb.is_coinbase or sum(a for _, a in cb.outputs) > BLOCK_REWARD + fees:
            raise InvalidBlock("bad coinbase")
    for j, out in enumerate(cb.outputs):
        new[f"{cb.txid}:{j}"] = out
    return new


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #
class Node:
    def __init__(self, name: str, genesis: Block, rule: str = "length",
                 tie_break: str = "first_seen", seed: int = 0,
                 labeler: Optional[Callable[[str], str]] = None):
        assert rule in ("work", "length") and tie_break in ("first_seen", "lowest_hash", "random")
        self.name, self.rule, self.tie_break = name, rule, tie_break
        self.rng = random.Random(seed)
        self.label = labeler or (lambda h: h[:8])
        self.blocks: Dict[str, Block] = {genesis.hash: genesis}
        self.cum_work: Dict[str, int] = {genesis.hash: genesis.work}
        self.utxo: Dict[str, Dict] = {genesis.hash: apply_block({}, genesis)}
        self.first_seen: Dict[str, float] = {genesis.hash: 0.0}
        self.orphans: Dict[str, List[Block]] = defaultdict(list)
        self.mempool: Dict[str, Tx] = {}
        self.tip = genesis.hash
        self.genesis = genesis.hash
        self.log: List[Tuple[float, str]] = []
        self.reorgs: List[dict] = []
        self.conflicted: List[str] = []           # txids reversed by a reorg and now invalid

    # ---------- chain helpers ----------
    def score(self, h: str) -> int:
        return self.cum_work[h] if self.rule == "work" else self.blocks[h].height

    def path(self, h: str) -> List[str]:
        out = []
        while True:
            out.append(h)
            if h == self.genesis:
                return out[::-1]
            h = self.blocks[h].prev_hash

    def main_chain(self) -> List[str]:
        return self.path(self.tip)

    def height(self) -> int:
        return self.blocks[self.tip].height

    def confirmations(self, txid: str) -> int:
        for h in reversed(self.main_chain()):
            if any(t.txid == txid for t in self.blocks[h].txs):
                return self.height() - self.blocks[h].height + 1
        return 0

    def balance(self, owner: str) -> int:
        return sum(a for o, a in self.utxo[self.tip].values() if o == owner)

    def _log(self, t: float, msg: str) -> None:
        self.log.append((t, msg))

    # ---------- mempool ----------
    def add_tx(self, tx: Tx) -> bool:
        spent = {c for m in self.mempool.values() for c in m.inputs}
        if any(c not in self.utxo[self.tip] or c in spent for c in tx.inputs):
            return False
        self.mempool[tx.txid] = tx
        return True

    def _prune_mempool(self) -> None:
        utxo = self.utxo[self.tip]
        in_chain = {t.txid for h in self.main_chain() for t in self.blocks[h].txs}
        self.mempool = {k: v for k, v in self.mempool.items()
                        if k not in in_chain and all(c in utxo for c in v.inputs)}

    # ---------- block processing ----------
    def receive(self, block: Block, t: float = 0.0, src: Optional[str] = None) -> List[Block]:
        """Process one block. Returns every block newly attached to the tree
        (the block itself plus any orphans it unlocked) so the caller can relay."""
        accepted, stack = [], [block]
        while stack:
            b = stack.pop()
            if self._accept_one(b, t, src):
                accepted.append(b)
                stack.extend(self.orphans.pop(b.hash, []))
        return accepted

    def _accept_one(self, b: Block, t: float, src: Optional[str]) -> bool:
        lab = self.label(b.hash)
        if b.hash in self.blocks:
            return False
        if not b.pow_ok():
            self._log(t, f"REJECT {lab}: invalid proof of work")
            return False
        if b.prev_hash not in self.blocks:
            if all(o.hash != b.hash for o in self.orphans[b.prev_hash]):
                self.orphans[b.prev_hash].append(b)
                self._log(t, f"ORPHAN {lab} (h={b.height}) from {src}: parent "
                             f"{self.label(b.prev_hash)} unknown, parked in orphan pool")
            return False
        parent = self.blocks[b.prev_hash]
        if b.height != parent.height + 1:
            self._log(t, f"REJECT {lab}: bad height")
            return False
        try:
            self.utxo[b.hash] = apply_block(self.utxo[parent.hash], b)
        except InvalidBlock as e:
            self._log(t, f"REJECT {lab}: {e}")
            return False
        self.blocks[b.hash] = b
        self.cum_work[b.hash] = self.cum_work[parent.hash] + b.work
        self.first_seen[b.hash] = t
        self._update_tip(b.hash, t, src)
        return True

    def _update_tip(self, h: str, t: float, src: Optional[str]) -> None:
        new, cur = self.score(h), self.score(self.tip)
        lab, tiplab = self.label(h), self.label(self.tip)
        if new > cur:
            self._switch(h, t)
        elif new == cur and h != self.tip:
            switch = (self.tie_break == "lowest_hash" and int(h, 16) < int(self.tip, 16)) or \
                     (self.tie_break == "random" and self.rng.random() < 0.5)
            self._log(t, f"TIE {lab} vs tip {tiplab} (score {new}); rule={self.tie_break} -> "
                         f"{'switch' if switch else 'keep ' + tiplab}")
            if switch:
                self._switch(h, t)
        else:
            self._log(t, f"STORE {lab} (h={self.blocks[h].height}) on side branch; tip stays {tiplab}")

    def _switch(self, h: str, t: float) -> None:
        old = self.tip
        if self.blocks[h].prev_hash == old:
            self.tip = h
            self._log(t, f"EXTEND tip -> {self.label(h)} (h={self.blocks[h].height})")
        else:
            old_path, new_path = self.path(old), self.path(h)
            k = 0
            while k < min(len(old_path), len(new_path)) and old_path[k] == new_path[k]:
                k += 1
            disconnected, connected = old_path[k:], new_path[k:]
            self.tip = h
            new_ids = {tx.txid for x in connected for tx in self.blocks[x].txs}
            utxo = self.utxo[h]
            back, reversed_ = [], []
            for x in disconnected:
                for tx in self.blocks[x].txs[1:]:
                    if tx.txid in new_ids:
                        continue
                    if all(c in utxo for c in tx.inputs):
                        self.mempool[tx.txid] = tx
                        back.append(tx.txid)
                    else:
                        reversed_.append(tx.txid)
                        self.conflicted.append(tx.txid)
            rec = dict(t=t, node=self.name, depth=len(disconnected),
                       fork_point=self.label(old_path[k - 1]),
                       disconnected=[self.label(x) for x in disconnected],
                       connected=[self.label(x) for x in connected],
                       txs_to_mempool=back, txs_reversed=reversed_)
            self.reorgs.append(rec)
            self._log(t, f"REORG depth {rec['depth']} at fork point {rec['fork_point']}: "
                         f"drop {rec['disconnected']} adopt {rec['connected']}"
                         + (f"; REVERSED txs {reversed_}" if reversed_ else ""))
        self._prune_mempool()

    # ---------- mining ----------
    def mine_block(self, t: float, txs: Optional[List[Tx]] = None, bits: int = DEFAULT_BITS,
                   payload_bytes: int = 0) -> Block:
        if txs is None:
            txs, spent, utxo = [], set(), self.utxo[self.tip]
            for tx in self.mempool.values():
                if all(c in utxo and c not in spent for c in tx.inputs):
                    txs.append(tx)
                    spent.update(tx.inputs)
        return mine(self.blocks[self.tip], self.name, txs, bits, t, payload_bytes)

    def summary(self) -> dict:
        return dict(node=self.name, tip=self.label(self.tip), height=self.height(),
                    cum_work=self.cum_work[self.tip],
                    chain=[self.label(h) for h in self.main_chain()],
                    known_blocks=len(self.blocks), orphans=sum(len(v) for v in self.orphans.values()),
                    reorgs=len(self.reorgs))


# --------------------------------------------------------------------------- #
# Discrete-event network
# --------------------------------------------------------------------------- #
class Network:
    def __init__(self, names: List[str], genesis: Optional[Block] = None, latency: float = 1.0,
                 bandwidth: Optional[float] = None, rule: str = "length",
                 tie_break: str = "first_seen", links: Optional[List[Tuple[str, str]]] = None,
                 seed: int = 0, rules: Optional[Dict[str, str]] = None):
        self.genesis = genesis or make_genesis()
        self.labels: Dict[str, str] = {self.genesis.hash: "G"}
        lab = lambda h: self.labels.get(h, h[:6])
        rules = rules or {}
        self.nodes: Dict[str, Node] = {
            n: Node(n, self.genesis, rules.get(n, rule), tie_break, seed + i, lab)
            for i, n in enumerate(names)}
        pairs = links or [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]
        self.links = {frozenset(p) for p in pairs}
        self.latency = {l: latency for l in self.links}
        self.bandwidth = bandwidth
        self.groups: Optional[List[set]] = None
        self.held: List[Tuple[str, str, Block]] = []
        self.q: list = []
        self.seq = 0
        self.now = 0.0

    # ---------- topology ----------
    def set_latency(self, a: str, b: str, sec: float) -> None:
        self.latency[frozenset((a, b))] = sec

    def neighbours(self, n: str) -> List[str]:
        return sorted(x for l in self.links if n in l for x in l if x != n)

    def partition(self, *groups: Iterable[str]) -> None:
        self.groups = [set(g) for g in groups]

    def heal(self) -> None:
        self.groups = None
        held, self.held = self.held, []
        for src, dst, b in held:
            self._send(src, dst, b)

    def _blocked(self, a: str, b: str) -> bool:
        return self.groups is not None and not any(a in g and b in g for g in self.groups)

    def delay(self, a: str, b: str, block: Block) -> float:
        d = self.latency[frozenset((a, b))]
        if self.bandwidth:
            d += block.size() / self.bandwidth
        return d

    # ---------- messaging ----------
    def _send(self, src: str, dst: str, block: Block) -> None:
        if self._blocked(src, dst):
            self.held.append((src, dst, block))
            return
        self.seq += 1
        heapq.heappush(self.q, (self.now + self.delay(src, dst, block), self.seq, src, dst, block))

    def broadcast(self, src: str, block: Block, exclude: Optional[str] = None) -> None:
        for peer in self.neighbours(src):
            if peer != exclude:
                self._send(src, peer, block)

    def name_block(self, b: Block, label: Optional[str] = None) -> str:
        base = label or f"{b.miner}{b.height}"
        lab, i = base, 1
        while lab in self.labels.values():
            i += 1
            lab = f"{base}.{i}"
        self.labels[b.hash] = lab
        return lab

    def mine(self, name: str, txs: Optional[List[Tx]] = None, bits: int = DEFAULT_BITS,
             payload_bytes: int = 0, broadcast: bool = True, label: Optional[str] = None) -> Block:
        node = self.nodes[name]
        b = node.mine_block(self.now, txs, bits, payload_bytes)
        self.name_block(b, label)
        node._log(self.now, f"MINED {self.labels[b.hash]} (h={b.height}, bits={b.bits})")
        node.receive(b, self.now, name)
        if broadcast:
            self.broadcast(name, b)
        return b

    def run(self, until: Optional[float] = None) -> None:
        while self.q and (until is None or self.q[0][0] <= until):
            t, _, src, dst, b = heapq.heappop(self.q)
            self.now = t
            for a in self.nodes[dst].receive(b, t, src):
                self.broadcast(dst, a, exclude=src)
        if until is not None:
            self.now = max(self.now, until)

    # ---------- reporting ----------
    def snapshot(self, tag: str = "") -> dict:
        return dict(tag=tag, t=round(self.now, 3),
                    nodes={n: nd.summary() for n, nd in self.nodes.items()},
                    converged=len({nd.tip for nd in self.nodes.values()}) == 1)

    def merged_log(self) -> List[str]:
        rows = [(t, n, m) for n, nd in self.nodes.items() for t, m in nd.log]
        rows.sort(key=lambda r: (r[0], r[1]))
        return [f"t={t:7.2f}s  {n}: {m}" for t, n, m in rows]


def print_snapshot(s: dict) -> None:
    print(f"\n[{s['tag']}] t={s['t']}s converged={s['converged']}")
    print(f"  {'node':<5}{'tip':<8}{'h':>3}{'work':>9}  main chain")
    for n, d in s["nodes"].items():
        print(f"  {n:<5}{d['tip']:<8}{d['height']:>3}{d['cum_work']:>9}  {' > '.join(d['chain'])}")


# --------------------------------------------------------------------------- #
# Scenarios (Part A)
# --------------------------------------------------------------------------- #
def scenario_fork(verbose: bool = True) -> dict:
    """(b) Two valid competing blocks at height 3, resolved by the next block."""
    net = Network(["A", "B", "C"], latency=1.0)
    net.set_latency("A", "C", 6.0)
    net.set_latency("B", "C", 3.0)
    net.mine("A"); net.run()
    net.mine("B"); net.run()
    snaps = [net.snapshot("common prefix")]
    net.mine("A")                      # A3
    net.mine("C")                      # C3, same instant, same parent: a fork
    net.run(until=net.now + 0.5)
    snaps.append(net.snapshot("fork created (before propagation)"))
    net.run()
    snaps.append(net.snapshot("after propagation: first-seen split"))
    net.mine("B")                      # B4 on top of A3
    net.run()
    snaps.append(net.snapshot("after B4: fork resolved"))
    out = dict(snapshots=snaps, log=net.merged_log(),
               reorgs=[r for nd in net.nodes.values() for r in nd.reorgs])
    if verbose:
        _print("SCENARIO 1: FORK AND LONGEST/HEAVIEST-CHAIN RESOLUTION", out)
    return out


def scenario_tie(verbose: bool = True) -> dict:
    """(c) Ties: equal-work competing tips under three tie-break policies."""
    results = {}
    for policy in ("first_seen", "lowest_hash", "random"):
        net = Network(["A", "B", "C"], latency=1.0, tie_break=policy, seed=11)
        net.set_latency("A", "C", 4.0)
        net.mine("B"); net.run()
        net.mine("A"); net.mine("C")
        net.run()
        s1 = net.snapshot(f"tie, policy={policy}")
        net.mine("B"); net.run()
        s2 = net.snapshot(f"after next block, policy={policy}")
        results[policy] = dict(during_tie=s1, after=s2, log=net.merged_log())
    if verbose:
        print("\n" + "=" * 78 + "\nSCENARIO 2: TIES\n" + "=" * 78)
        for p, r in results.items():
            print_snapshot(r["during_tie"]); print_snapshot(r["after"])
    return results


def scenario_delay(verbose: bool = True) -> dict:
    """(c) Delayed arrival: (i) child before parent (orphan pool) because a large
    block propagates slowly; (ii) partition, divergent mining, late delivery."""
    # (i) line topology A - B - C, bandwidth-limited links.
    net = Network(["A", "B", "C"], latency=0.5, bandwidth=50_000,
                  links=[("A", "B"), ("B", "C")])
    net.mine("A"); net.run()
    big = net.mine("A", payload_bytes=400_000, label="A2big")   # 8 s per hop
    net.run(until=net.now + 8.6)                                 # B has A2big, C does not
    net.mine("B", label="B3small")                               # small child, fast hop
    net.run()
    part1 = dict(snapshot=net.snapshot("out-of-order arrival resolved"), log=net.merged_log())

    # (ii) partition {A,B} | {C}
    net2 = Network(["A", "B", "C"], latency=1.0)
    net2.mine("A"); net2.run()
    net2.partition({"A", "B"}, {"C"})
    for m in ("A", "B", "A"):
        net2.mine(m); net2.run()
    for _ in range(2):
        net2.mine("C"); net2.run()
    split = net2.snapshot("during partition")
    net2.now += 30.0
    net2.heal(); net2.run()
    healed = net2.snapshot("after heal (late delivery)")
    part2 = dict(during=split, after=healed, log=net2.merged_log(),
                 reorgs=[r for nd in net2.nodes.values() for r in nd.reorgs])
    out = dict(out_of_order=part1, partition=part2)
    if verbose:
        print("\n" + "=" * 78 + "\nSCENARIO 3: DELAYED BLOCK ARRIVAL\n" + "=" * 78)
        print_snapshot(part1["snapshot"])
        for line in part1["log"]:
            if "ORPHAN" in line or "C:" in line:
                print("  " + line)
        print_snapshot(split); print_snapshot(healed)
        for r in part2["reorgs"]:
            print("  reorg:", r)
    return out


def scenario_heaviest(verbose: bool = True) -> dict:
    """Longest-chain vs heaviest-work: 3 easy blocks vs 2 hard blocks."""
    out = {}
    for rule in ("length", "work"):
        net = Network(["A", "B", "C"], latency=1.0, rule=rule)
        net.mine("B"); net.run()
        net.partition({"A"}, {"B", "C"})
        for _ in range(3):
            net.mine("A", bits=8); net.run()      # cheap branch: 3 x 2^8 = 768
        for _ in range(2):
            net.mine("C", bits=13); net.run()     # expensive branch: 2 x 2^13 = 16 384
        net.heal(); net.run()
        out[rule] = net.snapshot(f"rule={rule}")
    if verbose:
        print("\n" + "=" * 78 + "\nSCENARIO 4 (EXTENSION): LONGEST CHAIN vs HEAVIEST WORK\n" + "=" * 78)
        for s in out.values():
            print_snapshot(s)
    return out


def scenario_doublespend(z: int = 1, verbose: bool = True) -> dict:
    """R10 000 double-spend against a merchant who ships after z confirmations.
    Scripted: the attacker is assumed lucky enough to mine z+1 private blocks
    while the honest network mines z. Probabilities come from attacker_success()."""
    net = Network(["A", "B", "C"], latency=1.0)   # A honest miner, B merchant, C attacker
    A, B, C = (net.nodes[x] for x in "ABC")
    coin = next(cid for cid, (o, _) in A.utxo[A.tip].items() if o == "attacker")
    pay = Tx((coin,), (("merchant", 10_000),), "R10 000 purchase")
    back = Tx((coin,), (("attacker", 10_000),), "attacker refund to self")
    net.partition({"A", "B"}, {"C"})              # attacker mines in private from now
    net.mine("A", txs=[pay], label="A1pay")
    net.run()
    for _ in range(z - 1):
        net.mine("A"); net.run()
    shipped_at = B.confirmations(pay.txid)
    merchant_bal_before = B.balance("merchant")
    net.mine("C", txs=[back], label="C1dbl"); net.run()
    for _ in range(z):
        net.mine("C"); net.run()
    before = net.snapshot("merchant ships goods")
    net.heal(); net.run()
    after = net.snapshot("attacker releases private chain")
    out = dict(z=z, pay_txid=pay.txid, confirmations_when_shipped=shipped_at,
               merchant_balance_before=merchant_bal_before,
               merchant_balance_after=B.balance("merchant"),
               confirmations_after=B.confirmations(pay.txid),
               reversed_at_merchant=pay.txid in B.conflicted,
               before=before, after=after, reorgs=B.reorgs, log=net.merged_log())
    if verbose:
        print("\n" + "=" * 78 + f"\nSCENARIO 5: R10 000 DOUBLE-SPEND AGAINST z={z}\n" + "=" * 78)
        print_snapshot(before); print_snapshot(after)
        print(f"  merchant saw {shipped_at} conf(s), balance {merchant_bal_before} -> "
              f"{out['merchant_balance_after']}; payment reversed: {out['reversed_at_merchant']}")
    return out


# --------------------------------------------------------------------------- #
# Analytics: confirmation depth
# --------------------------------------------------------------------------- #
def nakamoto_success(q: float, z: int) -> float:
    """Nakamoto (2008) section 11: Poisson approximation of attacker progress."""
    p = 1.0 - q
    if q >= p:
        return 1.0
    lam = z * q / p
    s = 1.0
    for k in range(z + 1):
        poisson = math.exp(-lam) * lam ** k / math.factorial(k)
        s -= poisson * (1 - (q / p) ** (z - k))
    return max(0.0, s)


def rosenfeld_success(q: float, z: int) -> float:
    """Rosenfeld (2014) exact negative-binomial result (attacker must end strictly ahead)."""
    p = 1.0 - q
    if q >= p:
        return 1.0
    if z == 0:
        return 1.0
    s = 0.0
    for m in range(z + 1):
        s += math.comb(m + z - 1, m) * (p ** z * q ** m - p ** m * q ** z)
    return max(0.0, 1.0 - s)


def strict_success(q: float, z: int) -> float:
    """Exact success probability without a pre-mined block: the attacker starts on
    the parent of the payment block and must finish strictly ahead."""
    p = 1.0 - q
    if q >= p:
        return 1.0
    s = 0.0
    for m in range(z + 1):
        s += math.comb(m + z - 1, m) * p ** z * q ** m * (1 - (q / p) ** (z - m + 1))
    return max(0.0, 1.0 - s)


def attacker_success_mc(q: float, z: int, trials: int = 100_000, seed: int = 1,
                        give_up: int = 40, premine: int = 0) -> float:
    """Monte Carlo of the race. Each new block is the attacker's with prob q.
    premine=0: attacker starts on the parent of the payment block (strict model).
    premine=1: attacker already holds one private block (Rosenfeld's convention)."""
    rng = random.Random(seed)
    wins = 0
    for _ in range(trials):
        honest, att = 0, premine
        while honest < z:                         # merchant waits for z confirmations
            if rng.random() < q:
                att += 1
            else:
                honest += 1
        while True:                               # catch-up race (gambler's ruin)
            if att > honest:
                wins += 1
                break
            if honest - att > give_up:
                break
            if rng.random() < q:
                att += 1
            else:
                honest += 1
    return wins / trials


def required_depth(q: float, eps: float, zmax: int = 200) -> int:
    for z in range(1, zmax + 1):
        if rosenfeld_success(q, z) <= eps:
            return z
    return zmax


def simulate_stale_rate(block_interval: float, delay: float, n_miners: int = 10,
                        n_blocks: int = 20_000, seed: int = 0) -> float:
    """Abstract (no hashing) model used for Part B. Equal-power miners, uniform
    one-hop propagation delay; each miner mines on its first-seen highest tip."""
    rng = random.Random(seed)
    parent = {0: None}
    height = {0: 0}
    tip = [0] * n_miners
    pending: list = []
    t, nid = 0.0, 0
    for _ in range(n_blocks):
        t += rng.expovariate(1.0 / block_interval)
        while pending and pending[0][0] <= t:
            _, _, m, b = heapq.heappop(pending)
            if height[b] > height[tip[m]]:
                tip[m] = b
        m = rng.randrange(n_miners)
        nid += 1
        parent[nid], height[nid] = tip[m], height[tip[m]] + 1
        tip[m] = nid
        for o in range(n_miners):
            if o != m:
                heapq.heappush(pending, (t + delay, nid, o, nid))
    best = max(height, key=lambda b: (height[b], -b))
    main = 0
    while best is not None:
        main += 1
        best = parent[best]
    return 1.0 - (main - 1) / n_blocks


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print(title: str, out: dict) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)
    for s in out["snapshots"]:
        print_snapshot(s)
    print("\n  event log:")
    for line in out["log"]:
        print("  " + line)


def depth_table(verbose: bool = True) -> dict:
    qs, zs = (0.10, 0.20, 0.30), range(0, 11)
    rows = []
    for q in qs:
        for z in zs:
            rows.append(dict(q=q, z=z, nakamoto=nakamoto_success(q, z) if z else 1.0,
                             rosenfeld=rosenfeld_success(q, z),
                             mc_premine=attacker_success_mc(q, z, 20_000, seed=z, premine=1) if z else 1.0,
                             strict=strict_success(q, z) if z else 1.0,
                             mc_strict=attacker_success_mc(q, z, 20_000, seed=z) if z else 1.0))
    req = {q: {eps: required_depth(q, eps) for eps in (1e-2, 1e-3, 1e-4)} for q in qs}
    if verbose:
        print("\n" + "=" * 78 + "\nCONFIRMATION DEPTH: attacker success probability\n" + "=" * 78)
        print(f"  {'q':>5}{'z':>4}{'Nakamoto':>11}{'Rosenfeld':>11}{'MC(pre=1)':>11}"
              f"{'Strict':>11}{'MC(pre=0)':>11}")
        for r in rows:
            if r["z"] in (1, 2, 3, 6, 10):
                print(f"  {r['q']:>5}{r['z']:>4}{r['nakamoto']:>11.5f}{r['rosenfeld']:>11.5f}"
                      f"{r['mc_premine']:>11.5f}{r['strict']:>11.5f}{r['mc_strict']:>11.5f}")
        print("  required z (Rosenfeld):", req)
    return dict(rows=rows, required=req)


def _save(name: str, obj) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, f"{name}.json"), "w") as f:
        json.dump(obj, f, indent=2, default=str)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", nargs="?", default="all",
                    choices=["all", "fork", "tie", "delay", "heaviest", "doublespend", "depth"])
    ap.add_argument("--z", type=int, default=1, help="merchant confirmations for doublespend")
    a = ap.parse_args()
    jobs = {
        "fork": lambda: scenario_fork(),
        "tie": lambda: scenario_tie(),
        "delay": lambda: scenario_delay(),
        "heaviest": lambda: scenario_heaviest(),
        "doublespend": lambda: scenario_doublespend(a.z),
        "depth": lambda: depth_table(),
    }
    for k in (jobs if a.what == "all" else [a.what]):
        _save(k, jobs[k]())


if __name__ == "__main__":
    main()
