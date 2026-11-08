import copy, sys
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), ".."))
import bears_pbp as b  # noqa: E402
# Usage: python3 tests/simulate_live.py [ESPN_EVENT_ID]   (default: Bears @ Seahawks, Nov 2 2026)
# Feeds a finished game to the live tracker a few plays at a time and checks
# that every play/drive is sent exactly once and the final is reported.
EVENT = sys.argv[1] if len(sys.argv) > 1 else "401873036"
full = b.espn_json("summary", event=EVENT)
drives = full["drives"]["previous"]
allplays = [(i, j) for i, d in enumerate(drives) for j in range(len(d["plays"]))]
steps = list(range(0, len(allplays) + 1, 7)) + [len(allplays)]
state = {"i": 0, "pre": 2}
def snapshot():
    d = copy.deepcopy(full)
    st = d["header"]["competitions"][0]["status"]["type"]
    if state["pre"] > 0:
        state["pre"] -= 1
        st["state"] = "pre"; d["drives"] = {}; return d
    k = steps[min(state["i"], len(steps) - 1)]; state["i"] += 1
    if k < len(allplays):
        st["state"] = "in"
        di, pj = allplays[k] if k < len(allplays) else allplays[-1]
        prev = copy.deepcopy(drives[:di]); cur = copy.deepcopy(drives[di]); cur["plays"] = cur["plays"][:pj]
        for key in ("result", "displayResult"): cur.pop(key, None)
        d["drives"] = {"previous": prev, "current": cur}
    else:
        st["state"] = "post"
    return d
orig = b.espn_json
b.espn_json = lambda path, **kw: snapshot() if path == "summary" else orig(path, **kw)
b.time.sleep = lambda s: None
sent = []
class Cap(b.Sender):
    def send(self, t): sent.append(t); return True
b.track(EVENT, b.Outbox([Cap()]), poll=0)
texts = "\n\n".join(sent)
print("messages:", len(sent))
print("first:", sent[0][:120].replace("\n", " | "))
print("last:", sent[-1].replace("\n", " | "))
import collections
dups = [l for l, c in collections.Counter(texts.split("\n\n")).items() if c > 1 and "📋" not in l and "⏱️" not in l]
print("duplicate blocks:", len(dups), dups[:3])
print("drive summaries:", texts.count("📋"), "of", len(drives))
print("Bears scores / opponent scores:", texts.count("SCORE!"), texts.count("score:"))
assert not dups, "duplicate plays sent"
assert texts.count("📋") == len(drives), "missing drive summaries"
assert sent[-1].startswith("🏁"), "final not reported"
print("OK")
