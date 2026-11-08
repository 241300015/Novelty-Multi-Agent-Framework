"""Compare review prompts on frozen historical inputs using the configured local Qwen."""
import copy
import json
from pathlib import Path
import urllib.request

ROOT = Path("docs/experiments/planner-concept-stage")
OUTPUT = ROOT / "review-comparison-20261109"
OLD_SCHEMA = {
    "decisions": [{"candidate": 1, "decision": "pass|revise",
                   "terms_substitutable": True, "aliases_belong": True,
                   "source_supported": True, "reason": "理由"}],
    "missing": ["遗漏"]
}
NEW_SCHEMA = {
    "decisions": [{"candidate": 1, "source_supported": True, "source_quote": "输入原文片段",
                   "terms": [{"term": "词项", "substitutable": True, "reason": "理由"}],
                   "aliases": [{"alias": "别名", "substitutable": True, "reason": "理由"}]}],
    "missing": ["遗漏"]
}
OLD_PROMPT = "独立审查候选。逐项核对原文是否支持、同组 terms 能否分别替换、别名归属是否正确。想象仅命中其中一个词的文献，说明能否代表该概念。核心机制出现在另外一个词中不能弥补错误替代。整体审查还需检查核心机制遗漏，局部审查只评当前候选。根据分项结果给出决定、依据及具体修正理由。"
NEW_PROMPT = ("独立审查所有候选。逐项引用输入中的查新点原文，判断概念是否有依据。"
              "对每一个词项和每一个别名分别判断：只检索到该词时是否仍能表达候选的核心技术含义？"
              "给出具体理由。注意主词有依据不代表别名可以替代。空数组保持空数组。"
              "检查核心机制是否遗漏，只能引用输入中提供的原文，不要声称读过其他资料。"
              "输出严格 JSON，每个候选都要对应一条判断，terms 和 aliases 数组逐一覆盖输入。")


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cases = []
    for base in ("gsaeru-a1", "museformer-a1"):
        call = json.loads((ROOT / "live-20261109" / base / "calls/call-00002.json").read_text())
        payload = call["input"]
        old_schema = json.loads(call["messages"][1]["content"].split("输出 JSON 契约\n", 1)[1])
        cases.append((base + "-original", payload, {"kind": "natural_frozen"}, old_schema))
        injected = copy.deepcopy(payload)
        if base == "gsaeru-a1":
            removed = injected["candidates"].pop("1")
            spec = {"kind": "omission", "candidate": "1", "removed": removed}
        else:
            alias = "细粒度机制"
            injected["candidates"]["2"]["alias"].remove(alias)
            injected["candidates"]["3"]["alias"].append(alias)
            spec = {"kind": "alias_move", "alias": alias, "from": "2", "to": "3"}
        cases.append((base + "-injected", injected, spec, old_schema))

    for name, payload, injection, old_schema in cases:
        directory = OUTPUT / name
        directory.mkdir(exist_ok=True)
        save(directory / "input.json", payload)
        save(directory / "injection.json", injection)
        for version, prompt, schema in (("old", OLD_PROMPT, old_schema), ("new", NEW_PROMPT, NEW_SCHEMA)):
            messages = [{"role": "system", "content": prompt}, {"role": "user", "content":
                "任务输入\n" + json.dumps(payload, ensure_ascii=False) +
                "\n输出 JSON 契约（涵盖全部候选）：\n" + json.dumps(schema, ensure_ascii=False)}]
            request = {"model": "qwen2.5-7b-instruct", "messages": messages, "temperature": 0.2,
                       "max_tokens": 2048, "response_format": {"type": "json_object"}}
            save(directory / (version + "-request.json"), request)
            req = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions",
                data=json.dumps(request, ensure_ascii=False).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as response:
                    result = json.load(response)
                save(directory / (version + "-response.json"), result)
                print(name, version, result["choices"][0]["finish_reason"], flush=True)
                if result["choices"][0]["finish_reason"] != "stop":
                    print("Output truncated; stopping.", flush=True)
                    return
            except Exception as exc:
                save(directory / (version + "-error.json"), {"error": str(exc)})
                print(name, version, "error", type(exc).__name__, flush=True)
                return

if __name__ == "__main__":
    main()
