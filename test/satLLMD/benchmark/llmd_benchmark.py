#!/usr/bin/env python3
"""Emulate a user chatting with the llmd gateways (multi-turn chat session).

The context starts with a system prompt of constant length (--system-prompt-length words, the same for all sessions).
Each turn:
  1. a question (dummy words drawn from a vocabulary, default google-10000-english) is appended to the context;
  2. the gateway in is selected by llmd_gateway_selector.GatewaySelector.peek
  3. the whole context is sent to /v1/chat/completions, the answer is appended to the context;
  4. the user "thinks" for a random time before the next question.
The context therefore grows with every question and every answer received (the prefix caches of the llmd
workers see the previous context as a shared prefix).
When the context reaches --context-max tokens, the conversation is summarized: all questions and answers are
replaced by a summary (dummy words, random length, --summary-*), kept after the system prompt. The next question
then starts from this shorter context (the prefix changes, so the workers' prefix caches miss).
The first question of each session has a length uniform in [1, --context-max] words, so that parallel
sessions do not reach the summarization threshold at the same time.

Random variables (each with configurable mean and std):
  question length (words), answer length (max_tokens requested; the emulator's tokenizer counts words),
  think time (seconds).
Available distributions (--*-dist):
  lognormal  (default) positive and right-skewed, parametrized from mean/std. Heavy right tail, as observed for
             chat prompt/response lengths and for human reading/typing/think times.
  gamma      positive, right-skewed, lighter tail than lognormal. BurstGPT uses Gamma for request inter-arrivals.
  normal     truncated at the minimum (symmetric; what NVIDIA AIPerf uses for its turn-delay jitter).
std = 0 gives a constant.

Literature notes (not fitted here, the defaults are modelling choices, tune them to your trace):
  - BurstGPT (Wang et al., arXiv:2401.17644): request lengths ~ Zipf, response lengths bimodal/Zipf-like,
    request burstiness ~ Gamma. No session ids, so no think times.
  - ServeGen (Xiang et al., arXiv:2505.09999): characterizes production multi-turn conversations, including the
    inter-turn time distribution, and stresses reproducing it.
  - ShareGPT conversations: very skewed prompt lengths (median ~42 tokens, mean ~208), i.e. mean >> median.

usage: llmd_benchmark.py --sat-config llmd-sat-config.yaml [--access-file /tmp/srv6_access.json] [--turns 10] ...
"""
import argparse
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llmd_gateway_selector import GatewaySelector  # noqa: E402

DEFAULT_VOCAB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llmd_emu", "data", "google-10000-english.txt")


# ----------------------------
#   RANDOM VARIABLES
# ----------------------------
def make_sampler(dist: str, mean: float, std: float, minimum: float, rng: random.Random) -> Callable[[], float]:
    """Return a function sampling a positive random variable with the given mean and std (never below minimum)."""
    if std <= 0 or mean <= 0:
        return lambda: max(minimum, mean)
    if dist == "lognormal":
        s2 = math.log(1.0 + (std / mean) ** 2)          # variance of ln X
        mu, sigma = math.log(mean) - s2 / 2.0, math.sqrt(s2)
        return lambda: max(minimum, rng.lognormvariate(mu, sigma))
    if dist == "gamma":
        shape, scale = (mean / std) ** 2, std ** 2 / mean
        return lambda: max(minimum, rng.gammavariate(shape, scale))
    if dist == "normal":
        return lambda: max(minimum, rng.gauss(mean, std))
    raise ValueError(f"unknown distribution '{dist}'")


def load_vocabulary(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        words = [w for w in f.read().split() if w]
    if not words:
        raise ValueError(f"vocabulary file {path} contains no words")
    return words


# ----------------------------
#   CHAT SESSION
# ----------------------------
class SessionAbort(Exception):
    pass


def pick_gateway(selector: GatewaySelector, access_file: str, wait_s: float) -> str:
    """Gateway in the orbit of the access satellite; waits up to wait_s for one (not connected, handover...)."""
    deadline = time.time() + wait_s
    while True:
        gw = selector.peek(access_file)
        if gw:
            return gw
        if time.time() >= deadline:
            raise SessionAbort(f"no gateway available for the access satellite in {access_file}")
        time.sleep(1.0)


def post_chat(url: str, messages: List[dict], max_tokens: int, timeout: float) -> dict:
    body = json.dumps({"messages": messages, "max_tokens": max_tokens, "stream": False}).encode()
    req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def run_session(sid: int, args, selector: GatewaySelector, vocab: List[str], rng: random.Random,
                out_lock: threading.Lock, out_file, system_prompt: str = "") -> None:
    q_len = make_sampler(args.question_dist, args.question_mean, args.question_std, 1, rng)
    a_len = make_sampler(args.answer_dist, args.answer_mean, args.answer_std, 1, rng)
    think = make_sampler(args.think_dist, args.think_mean, args.think_std, 0.0, rng)
    summary_len = make_sampler(args.summary_dist, args.summary_mean, args.summary_std, 1, rng)
    # the context: system prompt (constant, same for all sessions) + user/assistant messages so far
    base_context: List[dict] = [{"role": "system", "content": system_prompt}] if system_prompt else []
    history: List[dict] = list(base_context)

    for turn in range(1, args.turns + 1):
        if turn == 1 and args.context_max > 1:
            # first question: uniform in [1, context_max] so parallel sessions reach the summarize threshold at different times
            n_words = rng.randint(1, args.context_max)
        else:
            n_words = int(round(q_len()))
        question = " ".join(rng.choices(vocab, k=n_words))
        max_tokens = int(round(a_len()))
        history.append({"role": "user", "content": question})

        resp, gw, t0 = None, "", time.time()
        for attempt in range(1, args.retries + 2):
            try:
                gw = pick_gateway(selector, args.access_file, args.gateway_wait)   # selected before every question
                t0 = time.time()
                resp = post_chat(f"http://{gw}:{args.gateway_port}/v1/chat/completions", history, max_tokens, args.timeout)
                break
            except SessionAbort as e:
                print(f"[s{sid} t{turn}] {e}", file=sys.stderr)
                return
            except urllib.error.HTTPError as e:
                if e.code == 400:                         # the context no longer fits in the KV pool
                    print(f"[s{sid} t{turn}] gateway {gw}: request too large (context full), session ends", file=sys.stderr)
                    return
                print(f"[s{sid} t{turn}] gateway {gw}: HTTP {e.code} (attempt {attempt})", file=sys.stderr)
            except (urllib.error.URLError, OSError, ValueError) as e:
                print(f"[s{sid} t{turn}] gateway {gw}: {e} (attempt {attempt})", file=sys.stderr)
            time.sleep(1.0)
        if resp is None:
            print(f"[s{sid} t{turn}] giving up after {args.retries + 1} attempts", file=sys.stderr)
            return
        latency = time.time() - t0

        answer = resp["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": answer})      # the answer grows the context
        usage, emu = resp.get("usage", {}), resp.get("llmd_emu", {})

        # context length (tokens) including the answer just received; measured by the gateway when reported
        context_len = (usage["prompt_tokens"] + usage.get("completion_tokens", 0)) if "prompt_tokens" in usage \
            else sum(len(m["content"].split()) for m in history)
        summarized = args.context_max > 0 and context_len >= args.context_max
        print(f"[s{sid} t{turn:>3}] gw={gw} q={len(question.split())}w max_tokens={max_tokens} "
              f"prompt={usage.get('prompt_tokens')} cached={usage.get('prompt_tokens_details', {}).get('cached_tokens')} "
              f"out={usage.get('completion_tokens')} ttft={emu.get('ttft_s')}s e2e={emu.get('e2e_s')}s "
              f"latency={latency:.3f}s context={context_len}{' -> summarized' if summarized else ''}", flush=True)
        if summarized:
            # the conversation so far is replaced by a summary (dummy words) placed after the system prompt
            summary = " ".join(rng.choices(vocab, k=int(round(summary_len()))))
            history = base_context + [{"role": "system", "content": summary}]
        if out_file:
            rec = {"session": sid, "turn": turn, "time": t0, "gateway": gw, "question_words": len(question.split()),
                   "max_tokens": max_tokens, "usage": usage, "llmd_emu": emu, "latency_s": latency,
                   "context_tokens": context_len, "summarized": summarized}
            with out_lock:
                out_file.write(json.dumps(rec) + "\n")
                out_file.flush()

        if turn < args.turns:
            time.sleep(think())                                         # user thinking time


# ----------------------------
#   MAIN
# ----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sat-config", required=True, help="llmd sat config yaml (e.g., llmd-sat-config.yaml); Etcd is reached "
                    "with the ETCD_ENDPOINT/ETCD_USER/ETCD_PASSWORD/ETCD_CA_CERT environment variables")
    ap.add_argument("--access-file", default="/tmp/srv6_access.json", help="file written by usr_srv6_learn.py")
    ap.add_argument("--gateway-port", type=int, default=8000, help="gateway port (default 8000)")
    ap.add_argument("--gateway-wait", type=float, default=30.0, help="max seconds to wait for a gateway (default 30)")
    ap.add_argument("--vocabulary-file", default=DEFAULT_VOCAB, help="one word per line (default google-10000-english)")
    ap.add_argument("--system-prompt-length", type=int, default=100,
                    help="length (words) of the constant system prompt placed at the start of every context; "
                         "the same prompt is used by all sessions (shared prefix). 0 = no system prompt (default 100)")
    ap.add_argument("--context-max", type=int, default=8000,
                    help="when the context (system prompt, questions, answers) reaches this length in tokens, the "
                         "conversation so far is replaced by a summary; 0 = never summarize (default 8000)")
    ap.add_argument("--summary-mean", type=float, default=200, help="summary length, words (default 200)")
    ap.add_argument("--summary-std", type=float, default=50)
    ap.add_argument("--summary-dist", choices=("lognormal", "gamma", "normal"), default="lognormal")
    ap.add_argument("--turns", type=int, default=10, help="questions per session (default 10)")
    ap.add_argument("--parallels", type=int, default=1, help="number of parallel independent chat sessions (default 1)")
    dists = ("lognormal", "gamma", "normal")
    ap.add_argument("--question-mean", type=float, default=50, help="question length, words (default 50)")
    ap.add_argument("--question-std", type=float, default=50)
    ap.add_argument("--question-dist", choices=dists, default="lognormal")
    ap.add_argument("--answer-mean", type=float, default=64, help="answer length, tokens requested as max_tokens (default 64)")
    ap.add_argument("--answer-std", type=float, default=64)
    ap.add_argument("--answer-dist", choices=dists, default="lognormal")
    ap.add_argument("--think-mean", type=float, default=10.0, help="think time between answer and next question, s (default 10)")
    ap.add_argument("--think-std", type=float, default=10.0)
    ap.add_argument("--think-dist", choices=dists, default="lognormal")
    ap.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout per request, s (default 120)")
    ap.add_argument("--retries", type=int, default=3, help="retries of a failed request (default 3)")
    ap.add_argument("--seed", type=int, help="random seed (reproducible sessions)")
    ap.add_argument("--output", help="append one JSON line per turn to this file")
    a = ap.parse_args()

    vocab = load_vocabulary(a.vocabulary_file)
    selector = GatewaySelector(a.sat_config)
    print(f"gateways: {selector.gateways}; vocabulary: {len(vocab)} words", flush=True)

    out_file = open(a.output, "a", encoding="utf-8") if a.output else None
    out_lock = threading.Lock()
    base = random.Random(a.seed)
    system_prompt = " ".join(base.choices(vocab, k=a.system_prompt_length)) if a.system_prompt_length > 0 else ""
    threads = []
    for sid in range(1, a.parallels + 1):
        rng = random.Random(base.random())                  # independent, reproducible stream per session
        t = threading.Thread(target=run_session, args=(sid, a, selector, vocab, rng, out_lock, out_file, system_prompt))
        threads.append(t)
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        if out_file:
            out_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
