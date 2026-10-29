"""真实 LLM 与文献接口。检索内容是线索，尚未经过原文核验。"""
from __future__ import annotations

import hashlib
import json
import os
import time
import warnings
from collections.abc import Iterator

import requests


class ServiceError(RuntimeError):
    """配置缺失或远端调用失败；错误信息不包含密钥和响应正文。"""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ServiceError(f"请配置环境变量 {name}")
    return value


def _post(url: str, key: str, payload: dict, *, service: str, timeout: float, proxy: str = "") -> dict:
    """每次只发送一个请求；错误标明服务，不包含地址、凭据或响应正文。"""
    proxies = None
    if proxy:
        proxy = proxy if "://" in proxy else "socks5h://" + proxy
        proxies = {"http": proxy, "https": proxy}
    try:
        response = requests.post(
            url, json=payload, timeout=timeout, proxies=proxies,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise ServiceError(f"{service} 返回 HTTP {status}",
                           retryable=status in (429, 500, 502, 503, 504)) from None
    except requests.RequestException as exc:
        transient = isinstance(exc, (requests.Timeout, requests.ConnectionError)) and not isinstance(exc, requests.exceptions.SSLError)
        raise ServiceError(f"{service} 网络请求失败：{type(exc).__name__}", retryable=transient) from None
    try:
        body = response.json()
    except ValueError:
        raise ServiceError(f"{service} 未返回有效 JSON") from None
    if not isinstance(body, dict):
        raise ServiceError(f"{service} 的 JSON 顶层必须是对象")
    return body


def ask(system: str, payload: dict) -> dict:
    """发送一次请求并返回 JSON；字符上限仅在用户显式配置时启用。"""
    base = _required("LITELLM_API_BASE").rstrip("/")
    key = _required("LITELLM_API_KEY")
    model = _required("STEEL_FOREST_LLM_MODEL")
    endpoint = os.getenv("STEEL_FOREST_LLM_ENDPOINT", "/chat/completions")
    url = base if base.endswith("/chat/completions") else base + "/" + endpoint.lstrip("/")
    user_text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    request_limit = os.getenv("STEEL_FOREST_MAX_REQUEST_CHARS", "").strip()
    if request_limit and len(system) + len(user_text) > int(request_limit):
        raise ServiceError("LLM 输入超出你设置的 STEEL_FOREST_MAX_REQUEST_CHARS；请调整或取消该上限")
    body = {
        "model": model, "temperature": 0,
        "messages": [
            {"role": "system", "content": system + "\n只返回一个 JSON 对象。"},
            {"role": "user", "content": user_text},
        ],
    }
    if os.getenv("STEEL_FOREST_LLM_RESPONSE_FORMAT", "json_object") != "none":
        body["response_format"] = {"type": "json_object"}
    if os.getenv("STEEL_FOREST_LLM_ENABLE_THINKING", "").lower() in {"0", "false", "no"}:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    response = _post(url, key, body, service="LLM",
        timeout=float(os.getenv("STEEL_FOREST_LLM_TIMEOUT_SEC", "180")),
        proxy=os.getenv("LITELLM_SOCKS5_PROXY", "").strip())
    try:
        choice = response["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ServiceError("LLM 输出被截断；本阶段未完成")
        content = choice["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if isinstance(content, str):
            content = content.strip()
            lines = content.splitlines()
            if len(lines) >= 3 and lines[0] in ("```", "```json") and lines[-1] == "```":
                content = "\n".join(lines[1:-1])
        result = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError):
        raise ServiceError("LLM 未返回有效 JSON 对象；本阶段未完成") from None
    if not isinstance(result, dict):
        raise ServiceError("LLM 输出顶层必须是 JSON 对象")
    return result


def _record(provider: str, raw: dict, *, text: str, status: str) -> dict:
    """保留供应方字段，缺少的题名、链接和正文保持为空。"""
    source = raw.get("source") or raw.get("paper_id") or raw.get("doi") or ""
    paper = raw.get("paper") if isinstance(raw.get("paper"), dict) else {}
    title = raw.get("title") or raw.get("paper_title") or paper.get("title") or ""
    url = raw.get("url") or raw.get("source_url") or paper.get("url") or ""
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return {
        "source_id": f"{provider}-{digest}", "source": source, "title": title,
        "text": text, "url": url, "provider": provider, "status": status,
        "authoritative_original": False, "raw": raw,
    }


def _lkm(query: str, top_k: int) -> list[dict]:
    base = os.getenv("LKM_API_BASE", "https://open.bohrium.com/openapi/v2/lkm").rstrip("/")
    endpoint = base if base.endswith("/search") else base + "/search"
    body = _post(endpoint, _required("BOHR_ACCESS_KEY"), {
        "query": query, "retrieval_mode": "hybrid", "sort_by": "comprehensive",
        "limit": top_k, "reasoning_only": False,
    }, service="LKM", timeout=60)
    if body.get("code") not in (0, None):
        raise ServiceError("LKM 接口报告调用失败")
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("variables"), list):
        raise ServiceError("LKM 响应缺少 data.variables")
    records = []
    for raw in data["variables"]:
        if not isinstance(raw, dict):
            raise ServiceError("LKM 文献线索不是 JSON 对象")
        content = next((raw[name] for name in ("text", "claim", "claim_text", "content", "description")
                        if isinstance(raw.get(name), str)), "")
        # 未知供应方字段保留为 JSON 线索，绝不将其改写成“原文”。
        text = content or json.dumps(raw, ensure_ascii=False)
        records.append(_record("lkm", raw, text=text, status="provider_lead_only"))
    return records


def _dprag(query: str, top_k: int) -> list[dict]:
    base = os.getenv("DPRAG_API_BASE",
        "https://funmg.dp.tech/sci-loop-dev-api/api/v1/literature/retrieve-context").rstrip("/")
    if base.endswith("/api/v1/literature/retrieve-context"):
        endpoint = base
    elif base.endswith("/api/v1"):
        endpoint = base + "/literature/retrieve-context"
    else:
        endpoint = base + "/api/v1/literature/retrieve-context"
    body = _post(endpoint, _required("DPRAG_API_KEY"), {"query": query, "top_k": top_k}, service="DPRAG", timeout=180)
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("context"), str):
        raise ServiceError("DPRAG 响应缺少 data.context")
    records = []
    for raw in data.get("retrieved_chunks") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            raise ServiceError("DPRAG 文献片段缺少文本")
        kind = "title_only" if str(raw.get("chunk_id", "")).startswith("title_") else "excerpt_unverified"
        records.append(_record("dprag", raw, text=raw["text"], status=kind))
    if not records and data["context"].strip():
        raw = {"context": data["context"]}
        records.append(_record("dprag", raw, text=data["context"], status="context_only_unverified"))
    return records


def literature(query: str, *, provider: str = "auto", top_k: int = 8,
               max_attempts: int = 3, on_event=None) -> Iterator[dict]:
    """文献临时故障有限重试，耗尽后继续；请求事件与真实文献分别返回。"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("文献查询不能为空")
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k 必须是正整数；服务自身的数量限制由服务端处理")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts 必须是正整数")
    providers = {"lkm": ("BOHR_ACCESS_KEY", _lkm), "dprag": ("DPRAG_API_KEY", _dprag)}
    if provider == "auto":
        selected = [name for name, (credential, _) in providers.items() if os.getenv(credential, "").strip()]
        if not selected:
            raise ServiceError("文献检索需配置 BOHR_ACCESS_KEY 或 DPRAG_API_KEY")
    elif provider in providers:
        selected = [provider]
    else:
        raise ValueError("文献 provider 必须是 auto、lkm 或 dprag")
    for name in selected:
        _required(providers[name][0])

        def notify(status, **details):
            event = {"provider": name, "attempt": attempt, "status": status, **details}
            if on_event is not None:
                on_event(event)
            elif status in ("retrying", "failed"):
                warnings.warn(f'{name.upper()} 第 {attempt} 次请求 {status}：{details["error"]}', RuntimeWarning)

        for attempt in range(1, max_attempts + 1):
            notify("running")
            try:
                records = providers[name][1](query.strip(), top_k)
            except ServiceError as error:
                retry = error.retryable and attempt < max_attempts
                notify("retrying" if retry else "failed", error=str(error), retryable=error.retryable)
                if not error.retryable:
                    raise
                if retry:
                    time.sleep(2 ** attempt)
                    continue
                break
            notify("succeeded", records=len(records))
            for record in records:
                yield {**record, "query": query.strip()}
            break
