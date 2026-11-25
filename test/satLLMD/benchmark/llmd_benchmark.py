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
When the context reaches --context-max tokens, the conversation is summarized, as a chat client does: a summary
request (the whole context plus a "summarize" instruction, max_tokens = random summary length, --summary-*) is sent
to the gateway, and all questions and answers are replaced by its answer, kept after the system prompt. The summary
runs in the background of the user's think time (the next question waits for the longer of the two); it is skipped
after the last turn. The next question then starts from this shorter context (the prefix changes, so the workers'
prefix caches miss). Summary requests are written to --output with "kind": "summary", turns with "kind": "turn".
The first question of each session has a length uniform in [1, --context-max] words, so that parallel
sessions do not reach the summarization threshold at the same time.

Random variables (each with configurable mean and std):
  question length (words), answer length (max_tokens requested; the emulator's tokenizer counts words),
  think time (seconds).
With --max-model-len set (the workers' worker.max_model_len), max_tokens is capped at max_model_len - prompt tokens,
as a real client does, so the vLLM-like emulator never rejects a request for exceeding the context length.
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

usage: llmd_benchmark.py [--config benchmark-config.yaml] --sat-config llmd-sat-config.yaml [--access-file /tmp/srv6_access.json] [--turns 10] ...
"""
import argparse
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llmd_gateway_selector import GatewaySelector  # noqa: E402

_TOKEN_RE = re.compile(r"\w+|[^\w\s]")   # the emulator's toy tokenizer (llmd_emu.kvcache): words and punctuation


def prompt_tokens(messages: List[dict]) -> int:
    """Prompt length in tokens as the emulator counts it (the gateway joins the message contents with newlines)."""
    return len(_TOKEN_RE.findall("\n".join(m["content"] for m in messages)))


DEFAULT_VOCAB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llmd_emu", "data", "google-10000-english.txt")

_t0_init: Optional[float] = None       # time of the first request of any parallel session
_t0_init_lock = threading.Lock()


def mark_first_request(t0: float) -> float:
    """Store t0 as t0_init if it is the first request of all sessions; return t0_init."""
    global _t0_init
    with _t0_init_lock:
        if _t0_init is None:
            _t0_init = t0
        return _t0_init


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


SUMMARY_INSTRUCTION = "Summarize the conversation so far, keeping the facts needed to continue it."


def run_session(sid: int, args, selector: GatewaySelector, vocab: List[str], rng: random.Random,
                out_lock: threading.Lock, out_file, system_prompt: str = "") -> None:
    q_len = make_sampler(args.question_dist, args.question_mean, args.question_std, 1, rng)
    a_len = make_sampler(args.answer_dist, args.answer_mean, args.answer_std, 1, rng)
    think = make_sampler(args.think_dist, args.think_mean, args.think_std, 0.0, rng)
    summary_len = make_sampler(args.summary_dist, args.summary_mean, args.summary_std, 1, rng)
    # the context: system prompt (constant, same for all sessions) + user/assistant messages so far
    base_context: List[dict] = [{"role": "system", "content": system_prompt}] if system_prompt else []
    history: List[dict] = list(base_context)

    def send(tag: str, messages: List[dict], max_tokens: int):
        """POST one chat request with retries; the gateway is selected before every attempt.
        Returns (response, gateway, t0, latency_s), or None if the session must end."""
        gw = ""
        for attempt in range(1, args.retries + 2):
            try:
                gw = pick_gateway(selector, args.access_file, args.gateway_wait)
                t0 = time.time()
                mark_first_request(t0)
                resp = post_chat(f"http://{gw}:{args.gateway_port}/v1/chat/completions", messages, max_tokens, args.timeout)
                return resp, gw, t0, time.time() - t0
            except SessionAbort as e:
                print(f"[s{sid} {tag}] {e}", file=sys.stderr)
                return None
            except urllib.error.HTTPError as e:
                if e.code == 400:                         # context longer than max_model_len or the KV pool
                    print(f"[s{sid} {tag}] gateway {gw}: request too large (context full), session ends", file=sys.stderr)
                    return None
                print(f"[s{sid} {tag}] gateway {gw}: HTTP {e.code} (attempt {attempt})", file=sys.stderr)
            except (urllib.error.URLError, OSError, ValueError) as e:
                print(f"[s{sid} {tag}] gateway {gw}: {e} (attempt {attempt})", file=sys.stderr)
            time.sleep(1.0)
        print(f"[s{sid} {tag}] giving up after {args.retries + 1} attempts", file=sys.stderr)
        return None

    def write(rec: dict) -> None:
        if out_file:
            with out_lock:
                out_file.write(json.dumps(rec) + "\n")
                out_file.flush()

    def summarize(turn: int) -> Optional[List[dict]]:
        """Summary request over the whole context; returns the new history (system prompt + summary), or None."""
        requested = int(round(summary_len()))
        convo = history[len(base_context):] # the user/assistant messages so far, to be summarized
        instr = {"role": "user", "content": SUMMARY_INSTRUCTION}
        messages = base_context + convo + [instr]
        max_tokens = requested
        if args.max_model_len > 0:
            # like a client with a full window: drop the oldest messages until the summary fits (keep the last one)
            while len(convo) > 1 and prompt_tokens(messages) + requested > args.max_model_len:
                # drop the oldest message (the first in the conversation, after the system prompt)
                convo = convo[1:]
                messages = base_context + convo + [instr]
            max_tokens = max(1, min(requested, args.max_model_len - prompt_tokens(messages)))
        sent = send(f"t{turn} summary", messages, max_tokens)
        if sent is None:
            return None
        resp, gw, t0, latency = sent
        summary = resp["choices"][0]["message"]["content"]
        new_history = base_context + [{"role": "system", "content": summary}]
        usage, emu = resp.get("usage", {}), resp.get("llmd_emu", {})
        context_len = prompt_tokens(new_history)
        print(f"[s{sid} t{turn:>3}] summary gw={gw} dropped={len(history) - len(base_context) - len(convo)}msg "
              f"max_tokens={max_tokens}{f' (capped from {requested})' if max_tokens < requested else ''} "
              f"prompt={usage.get('prompt_tokens')} cached={usage.get('prompt_tokens_details', {}).get('cached_tokens')} "
              f"out={usage.get('completion_tokens')} ttft={emu.get('ttft_s')}s e2e={emu.get('e2e_s')}s "
              f"latency={latency:.3f}s context={context_len}", flush=True)
        write({"kind": "summary", "session": sid, "turn": turn, "time": t0 - mark_first_request(t0), "gateway": gw,
               "question_words": 0, "max_tokens": max_tokens, "max_tokens_requested": requested, "usage": usage,
               "llmd_emu": emu, "latency_s": latency, "context_tokens": context_len, "summarized": False})
        return new_history

    for turn in range(1, args.turns + 1):
        if turn == 1 and args.context_max > 1:
            # first question: uniform in [1, context_max] so parallel sessions reach the summarize threshold at different times
            n_words = rng.randint(1, args.context_max)
        else:
            n_words = int(round(q_len()))
        question = " ".join(rng.choices(vocab, k=n_words))
        max_tokens = requested_tokens = int(round(a_len()))
        history.append({"role": "user", "content": question})
        if args.max_model_len > 0:
            n_prompt = prompt_tokens(history)
            if n_prompt >= args.max_model_len:     # no room even for one token: shorten the question
                drop = n_prompt - args.max_model_len + 1
                words = question.split()
                question = " ".join(words[:max(len(words) - drop, 0)])
                history[-1]["content"] = question
                n_prompt = prompt_tokens(history)
            max_tokens = max(1, min(max_tokens, args.max_model_len - n_prompt))

        sent = send(f"t{turn}", history, max_tokens)
        if sent is None:
            return
        resp, gw, t0, latency = sent

        answer = resp["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": answer})      # the answer grows the context
        usage, emu = resp.get("usage", {}), resp.get("llmd_emu", {})

        # context length (tokens) including the answer just received; measured by the gateway when reported
        context_len = (usage["prompt_tokens"] + usage.get("completion_tokens", 0)) if "prompt_tokens" in usage \
            else sum(len(m["content"].split()) for m in history)
        summarized = args.context_max > 0 and context_len >= args.context_max
        print(f"[s{sid} t{turn:>3}] gw={gw} q={len(question.split())}w max_tokens={max_tokens}"
              f"{f' (capped from {requested_tokens})' if max_tokens < requested_tokens else ''} "
              f"prompt={usage.get('prompt_tokens')} cached={usage.get('prompt_tokens_details', {}).get('cached_tokens')} "
              f"out={usage.get('completion_tokens')} ttft={emu.get('ttft_s')}s e2e={emu.get('e2e_s')}s "
              f"latency={latency:.3f}s context={context_len}{' -> summarized' if summarized else ''}", flush=True)
        write({"kind": "turn", "session": sid, "turn": turn, "time": t0 - mark_first_request(t0), "gateway": gw,
               "question_words": len(question.split()), "max_tokens": max_tokens, "max_tokens_requested": requested_tokens,
               "usage": usage, "llmd_emu": emu, "latency_s": latency, "context_tokens": context_len,
               "summarized": summarized})

        if turn < args.turns:
            think_until = time.time() + think()                         # user thinking time
            if summarized:
                # the client summarizes while the user thinks: the conversation so far is replaced by the
                # summary (the model's answer to the summary request), placed after the system prompt
                history = summarize(turn)
                if history is None:
                    return
            time.sleep(max(0.0, think_until - time.time()))


def load_config(ap: argparse.ArgumentParser, path: str) -> Dict:
    """Read a yaml of option values (keys = option names without '--'; '-' and '_' both accepted).
    Null values are skipped (the built-in default is kept). Returns {dest: value} for ap.set_defaults."""
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        ap.error(f"{path}: expected a mapping of option: value")
    actions = {act.dest: act for act in ap._actions if act.dest not in ("help", "config")}
    out = {}
    for key, val in cfg.items():
        dest = str(key).replace("-", "_")
        act = actions.get(dest)
        if act is None:
            ap.error(f"{path}: unknown option '{key}'")
        if val is None:
            continue
        if act.type is not None:
            try:
                val = act.type(val)
            except (TypeError, ValueError):
                ap.error(f"{path}: invalid value for '{key}': {val!r}")
        if act.choices is not None and val not in act.choices:
            ap.error(f"{path}: '{key}' must be one of {', '.join(map(str, act.choices))}")
        out[dest] = val
    return out


# ----------------------------
#   MAIN
# ----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="benchmark config yaml (e.g., benchmark-config.yaml) with any of the options below "
                    "(keys = option names without '--'); options given on the command line override the file")
    ap.add_argument("--sat-config", help="(required, here or in --config) "
                    "llmd sat config yaml (e.g., llmd-sat-config.yaml); Etcd is reached "
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
    ap.add_argument("--summary-mean", type=float, default=200,
                    help="summary length, tokens requested as max_tokens of the summary request (default 200)")
    ap.add_argument("--summary-std", type=float, default=50)
    ap.add_argument("--summary-dist", choices=("lognormal", "gamma", "normal"), default="lognormal")
    ap.add_argument("--turns", type=int, default=10, help="questions per session (default 10)")
    ap.add_argument("--parallels", type=int, default=1, help="number of parallel independent chat sessions (default 1)")
    dists = ("lognormal", "gamma", "normal")
    ap.add_argument("--question-mean", type=float, default=50, help="question length, words (default 50)")
    ap.add_argument("--question-std", type=float, default=50)
    ap.add_argument("--question-dist", choices=dists, default="lognormal")
    ap.add_argument("--answer-mean", type=float, default=300, help="answer length, tokens requested as max_tokens (default 300)")
    ap.add_argument("--answer-std", type=float, default=20)
    ap.add_argument("--answer-dist", choices=dists, default="lognormal")
    ap.add_argument("--max-model-len", type=int, default=0,
                    help="the workers' max_model_len: max_tokens is capped at max_model_len - prompt tokens; "
                         "0 = no cap (default 0)")
    ap.add_argument("--think-mean", type=float, default=10.0, help="think time between answer and next question, s (default 10)")
    ap.add_argument("--think-std", type=float, default=10.0)
    ap.add_argument("--think-dist", choices=dists, default="lognormal")
    ap.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout per request, s (default 120)")
    ap.add_argument("--retries", type=int, default=3, help="retries of a failed request (default 3)")
    ap.add_argument("--seed", type=int, help="random seed (reproducible sessions)")
    ap.add_argument("--output", help="write one JSON line per turn to this file")
    pre, _ = ap.parse_known_args()
    if pre.config:
        ap.set_defaults(**load_config(ap, pre.config))        # file values become defaults, the command line wins
    a = ap.parse_args()
    if not a.sat_config:
        ap.error("--sat-config is required (on the command line or in --config)")

    vocab = load_vocabulary(a.vocabulary_file)
    selector = GatewaySelector(a.sat_config)
    print(f"gateways: {selector.gateways}; vocabulary: {len(vocab)} words", flush=True)

    out_file = open(a.output, "w", encoding="utf-8") if a.output else None
    out_lock = threading.Lock()
    base = random.Random(a.seed)
    system_prompt = " ".join(base.choices(vocab, k=a.system_prompt_length)) if a.system_prompt_length > 0 else ""
    threads = []
    for sid in range(1, a.parallels + 1):
        rng = random.Random(base.random())                  # independent, reproducible stream per session
        t = threading.Thread(target=run_session, args=(sid, a, selector, vocab, rng, out_lock, out_file, system_prompt))
        threads.append(t)
        t.start()
        time.sleep(2)  # stagger the sessions a bit so they don't all start at the same time
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
