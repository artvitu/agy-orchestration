#!/usr/bin/env python3
"""Общий помощник определения активной модели, пула квот и порога контекстного окна
для `agy-hook-adapter.py` и `context-meter.py` в Google Antigravity 2.x.

Единая логика с боковым плагином `context-sidecar/main.mjs`:
  1) быстрый разбор `<USER_SETTINGS_CHANGE>` из `transcript.jsonl`;
  2) кэшируемый опрос локального `LanguageServerService` (`GetUserStatus`,
     `GetCascadeTrajectoryGeneratorMetadata` с `generatorMetadataOffset`);
  3) резервная таблица порогов `512k` / `256k` / `160k` / `128k` / `80k`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request


BASE_CEILING = 256000
ARGON_CEILING = 512000
CLAUDE_OPUS_CEILING = 840000
M26_CEILING = 160000
GEMINI25_CEILING = 128000
GPT_OSS_CEILING = 80000

_UI_MODEL_RE = re.compile(
    r"The user changed setting\s+`?Model Selection`?\s+from\s+.+?\s+to\s+([^\n]+?)(?:\.\s+(?:[A-ZА-Я]|$)|$)",
    re.IGNORECASE,
)
_SETTINGS_BLOCK_RE = re.compile(
    r"<USER_SETTINGS_CHANGE>([\s\S]*?)</USER_SETTINGS_CHANGE>",
    re.IGNORECASE,
)
_THIRD_PARTY_RE = re.compile(r"\b(claude|opus|sonnet|haiku|gpt|openai|anthropic)\b", re.IGNORECASE)
_CLAUDE_OPUS_RE = re.compile(
    r"(?:\bclaude[\s_-]*(?:opus|sonnet[\s_-]*5[._-]5)\b|(?<![A-Za-z0-9])(?:M400|M401|M402|M403|M404|M405)\b)",
    re.IGNORECASE,
)
_CARBON_LOW_RE = re.compile(
    r"(?:carbon\s*\(low\)|(?<![A-Za-z0-9])(?:M408|M438)\b)",
    re.IGNORECASE,
)
_ARGON_RE = re.compile(
    r"(?:\b(?:argon|hydrogen|neon|carbon)\b|(?<![A-Za-z0-9])(?:M417|M418|M419|M427|M432|M437|M525|M526|M527|M528|M532|M533|M534|M535|M537|M667|M681)\b)",
    re.IGNORECASE,
)
_GPT_OSS_RE = re.compile(r"gpt-oss|OPENAI_GPT_OSS", re.IGNORECASE)
_GEMINI25_RE = re.compile(r"gemini\s*2\.5|GEMINI_2_5", re.IGNORECASE)
_M26_RE = re.compile(r"(?:\bMODEL_PLACEHOLDER_M26\b|(?<![A-Za-z0-9])M26\b)", re.IGNORECASE)
_BASE256_RE = re.compile(
    r"(?:\bclaude\b|(?<![A-Za-z0-9])M124\b)",
    re.IGNORECASE,
)


def safe_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:120] or "default"


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass


def parse_ui_model_selection(raw_user_text: str) -> str:
    """Извлекает имя модели из служебного блока `<USER_SETTINGS_CHANGE>` в `USER_INPUT`."""
    if not raw_user_text or "USER_SETTINGS_CHANGE" not in raw_user_text:
        return ""
    blk = _SETTINGS_BLOCK_RE.search(raw_user_text)
    if not blk:
        return ""
    m = _UI_MODEL_RE.search(blk.group(1))
    if not m:
        return ""
    return re.sub(r"\.\s*$", "", m.group(1)).strip()


def is_third_party_text(s: str) -> bool:
    return bool(_THIRD_PARTY_RE.search(str(s or "")))


def format_fallback_model_name(response_model: str, code: str) -> str:
    raw = str(response_model or code or "").strip()
    if not raw:
        return ""
    if raw.upper().startswith("MODEL_PLACEHOLDER_"):
        return "Model " + raw[len("MODEL_PLACEHOLDER_") :]
    if raw.upper().startswith("MODEL_OPENAI_GPT_OSS"):
        return "GPT-OSS 120B"
    cleaned = re.sub(r"^models/", "", raw)
    return " ".join(w[0].upper() + w[1:] for w in cleaned.split("-") if w)


def fallback_ceiling_by_model(
    model_label: str,
    model_code: str,
    max_total: int = 0,
    default_ceiling: int = BASE_CEILING,
) -> int:
    txt = f"{model_label or ''} {model_code or ''}"
    if _CLAUDE_OPUS_RE.search(txt):
        return CLAUDE_OPUS_CEILING
    if _CARBON_LOW_RE.search(txt):
        return BASE_CEILING
    if _ARGON_RE.search(txt):
        return ARGON_CEILING
    if _GPT_OSS_RE.search(txt):
        return GPT_OSS_CEILING
    if _GEMINI25_RE.search(txt):
        return GEMINI25_CEILING
    if _M26_RE.search(txt):
        return M26_CEILING
    if _BASE256_RE.search(txt):
        return BASE_CEILING
    if max_total > 530000:
        return CLAUDE_OPUS_CEILING
    return ARGON_CEILING if max_total > 265000 else default_ceiling


def post_ls_rpc_strict(
    addr: str,
    csrf_tok: str,
    method: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, Any]:
    """Вызывает RPC `LanguageServerService` и возвращает `(http_status, json_или_ошибка)`.

    При сетевой ошибке / таймауте возвращает `(0, текст_ошибки)`.
    """
    if not addr or not csrf_tok:
        return 0, "отсутствует адрес или CSRF-токен Language Server"
    req = urllib.request.Request(
        f"http://{addr}/exa.language_server_pb.LanguageServerService/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-codeium-csrf-token": csrf_tok},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw_txt = r.read().decode("utf-8").strip()
            return int(r.status), (json.loads(raw_txt) if raw_txt else {})
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace").strip()
            code_http = int(exc.code)
        finally:
            exc.close()
        try:
            return code_http, json.loads(err_body)
        except Exception:
            return code_http, err_body
    except Exception as exc:
        return 0, str(exc)


def _post_ls_rpc(addr: str, csrf_tok: str, method: str, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    code, body = post_ls_rpc_strict(addr, csrf_tok, method, payload, timeout)
    if code == 200 and isinstance(body, dict):
        return body
    return None


_LAST_DISCOVERY_ERROR = ""
_SANDBOX_EPERM_MSG = (
    "Доступ к локальному Antigravity Language Server (127.0.0.1) заблокирован сетевой песочницей macOS "
    "(Operation not permitted). Переключи сессию-отправителя в режим TURBO "
    "(agy-wake.py preset --to <id> --preset turbo --yes из обычного терминала или в настройках сессии) "
    "либо вызови команду с BypassSandbox."
)


def _is_sandbox_eperm(err: Any) -> bool:
    txt = str(err or "")
    return bool(re.search(r"Operation not permitted|\[Errno 1\]", txt, re.IGNORECASE))


def _discover_from_daemon_dir(timeout: float) -> tuple[str, str] | None:
    global _LAST_DISCOVERY_ERROR
    daemon_dir = Path(os.environ.get("ANTIGRAVITY_DAEMON_DIR") or (Path.home() / ".gemini/antigravity/daemon"))
    if not daemon_dir.is_dir():
        return None
    candidates = sorted(
        daemon_dir.glob("hub_*.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    saw_eperm = False
    for fpath in candidates:
        try:
            info = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        http_port = info.get("httpPort")
        csrf = str(info.get("csrfToken") or info.get("csrf_token") or "").strip()
        if not http_port or not csrf:
            continue
        cand_addr = f"127.0.0.1:{http_port}"
        code, body = post_ls_rpc_strict(cand_addr, csrf, "RetrieveUserQuotaSummary", {}, timeout)
        if code == 200 and isinstance(body, dict):
            _LAST_DISCOVERY_ERROR = ""
            return cand_addr, csrf
        if code == 0 and _is_sandbox_eperm(body):
            saw_eperm = True
    if saw_eperm:
        _LAST_DISCOVERY_ERROR = _SANDBOX_EPERM_MSG
    return None


def _discover_ls_conn(state_dir: Path | None, timeout: float = 1.0) -> tuple[str, str] | None:
    global _LAST_DISCOVERY_ERROR
    _LAST_DISCOVERY_ERROR = ""
    env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if env_addr and env_csrf:
        return env_addr, env_csrf

    conn_file = (state_dir / "ls-conn.json") if state_dir else None
    if conn_file and conn_file.is_file():
        try:
            cinfo = json.loads(conn_file.read_text(encoding="utf-8"))
            addr = str(cinfo.get("addr") or "").strip()
            csrf = str(cinfo.get("csrf") or "").strip()
            if addr and csrf:
                return addr, csrf
        except Exception:
            pass

    daemon_conn = _discover_from_daemon_dir(timeout)
    if daemon_conn:
        if conn_file:
            _write_private_json(conn_file, {"addr": daemon_conn[0], "csrf": daemon_conn[1], "ts": time.time()})
        return daemon_conn
    if _LAST_DISCOVERY_ERROR:
        return None

    try:
        ps_out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=1.5).stdout
        for line in ps_out.splitlines():
            if "language_server" not in line or "--csrf_token" not in line:
                continue
            pm = re.match(r"^\s*(\d+)", line)
            cm = re.search(r"(?<!extension_server_)--csrf_token[=\s]+([A-Za-z0-9\-_.]+)", line)
            if not pm or not cm:
                continue
            pid, cand_csrf = pm.group(1), cm.group(1)
            lsof_out = subprocess.run(
                ["lsof", "-iTCP", "-sTCP:LISTEN", "-P", "-n", "-a", "-p", pid],
                capture_output=True,
                text=True,
                timeout=1.5,
            ).stdout
            for port_m in re.finditer(r":(\d{4,5})\b", lsof_out):
                cand_addr = f"127.0.0.1:{port_m.group(1)}"
                code, body = post_ls_rpc_strict(cand_addr, cand_csrf, "RetrieveUserQuotaSummary", {}, timeout)
                if code == 200 and isinstance(body, dict):
                    if conn_file:
                        _write_private_json(conn_file, {"addr": cand_addr, "csrf": cand_csrf, "ts": time.time()})
                    return cand_addr, cand_csrf
                if code == 0 and _is_sandbox_eperm(body):
                    _LAST_DISCOVERY_ERROR = _SANDBOX_EPERM_MSG
    except Exception:
        pass
    return None


def call_ls_rpc_strict(
    method: str,
    payload: dict[str, Any],
    state_dir: Path | None = None,
    timeout: float = 5.0,
) -> tuple[int, Any]:
    """Вызывает RPC локального `LanguageServerService` с возвратом `(http_status, body)`."""
    conn = _discover_ls_conn(state_dir, timeout=min(timeout, 2.0))
    if not conn:
        return 0, (_LAST_DISCOVERY_ERROR or "Antigravity Language Server не найден (проверь, запущен ли Antigravity Desktop)")
    addr, csrf = conn
    code, body = post_ls_rpc_strict(addr, csrf, method, payload, timeout=timeout)
    if code == 0 and _is_sandbox_eperm(body):
        return 0, _SANDBOX_EPERM_MSG
    if code not in (0, 401, 403):
        return code, body

    conn_file = (state_dir / "ls-conn.json") if state_dir else None
    if conn_file and conn_file.is_file() and not os.environ.get("ANTIGRAVITY_LS_ADDRESS"):
        try:
            conn_file.unlink()
        except OSError:
            pass
    fresh = _discover_from_daemon_dir(min(timeout, 2.0))
    if fresh and fresh != (addr, csrf):
        if conn_file and not os.environ.get("ANTIGRAVITY_LS_ADDRESS"):
            _write_private_json(conn_file, {"addr": fresh[0], "csrf": fresh[1], "ts": time.time()})
        code2, body2 = post_ls_rpc_strict(fresh[0], fresh[1], method, payload, timeout=timeout)
        if code2 == 0 and _is_sandbox_eperm(body2):
            return 0, _SANDBOX_EPERM_MSG
        return code2, body2
    if _LAST_DISCOVERY_ERROR:
        return 0, _LAST_DISCOVERY_ERROR
    return code, body


def call_ls_rpc(
    method: str,
    payload: dict[str, Any],
    state_dir: Path | None = None,
    timeout: float = 1.0,
) -> dict[str, Any] | None:
    code, body = call_ls_rpc_strict(method, payload, state_dir=state_dir, timeout=timeout)
    if code == 200 and isinstance(body, dict):
        return body
    return None


def fetch_model_catalog(
    state_dir: Path | None = None,
    ttl_sec: int = 300,
    timeout: float = 1.0,
) -> dict[str, Any]:
    empty = {"by_code": {}, "by_label": {}, "default_code": "", "default_label": ""}
    cache_file = (state_dir / "model-catalog-cache.json") if state_dir else None
    now_ts = time.time()
    cached: dict[str, Any] | None = None
    if cache_file and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now_ts - float(cached.get("ts") or 0) < ttl_sec and cached.get("by_code"):
                return cached
        except Exception:
            cached = None

    raw = call_ls_rpc("GetUserStatus", {}, state_dir=state_dir, timeout=timeout)
    if not raw:
        return cached or empty

    data = ((raw.get("userStatus") or {}).get("cascadeModelConfigData")) or {}
    by_code: dict[str, dict[str, str]] = {}
    by_label: dict[str, dict[str, str]] = {}
    for cfg in data.get("clientModelConfigs") or []:
        moa = cfg.get("modelOrAlias") or {}
        code = str(moa.get("model") or moa.get("alias") or "").strip()
        label = str(cfg.get("label") or "").strip()
        model_id = str(cfg.get("modelId") or "").strip()
        if not code and not label:
            continue
        pool = "3p" if is_third_party_text(f"{label} {model_id} {code}") else "google"
        entry = {"code": code, "label": label, "modelId": model_id, "pool": pool}
        if code:
            by_code[code] = entry
        if label:
            by_label[label.lower()] = entry

    default_code = str(((data.get("defaultOverrideModelConfig") or {}).get("modelOrAlias") or {}).get("model") or "")
    default_label = (by_code.get(default_code) or {}).get("label", "")
    catalog = {
        "ts": now_ts,
        "by_code": by_code,
        "by_label": by_label,
        "default_code": default_code,
        "default_label": default_label,
    }
    if cache_file and by_code:
        _write_private_json(cache_file, catalog)
    return catalog


def fetch_trajectory_meta(
    conv_id: str,
    state_dir: Path | None = None,
    last_planner_step: int = -1,
    ttl_sec: int = 30,
    timeout: float = 1.0,
) -> dict[str, Any] | None:
    if not conv_id:
        return None
    cache_file = (state_dir / "traj-meta" / f"{safe_id(conv_id)}.json") if state_dir else None
    now_ts = time.time()
    prev: dict[str, Any] | None = None
    if cache_file and cache_file.is_file():
        try:
            prev = json.loads(cache_file.read_text(encoding="utf-8"))
            prev_step = int(prev.get("last_planner_step", -1))
            prev_meta = prev.get("meta")
            if prev_meta and last_planner_step >= 0 and prev_step == last_planner_step:
                if now_ts - float(prev.get("ts") or 0) < ttl_sec:
                    return prev_meta
        except Exception:
            prev = None

    prev_gm_count = int((prev or {}).get("gm_count") or 0)
    offset = max(0, prev_gm_count - 2) if prev_gm_count > 2 else 0
    raw = call_ls_rpc(
        "GetCascadeTrajectoryGeneratorMetadata",
        {"cascadeId": conv_id, "generatorMetadataOffset": offset},
        state_dir=state_dir,
        timeout=timeout,
    )
    if raw is None:
        return (prev or {}).get("meta")

    gms = raw.get("generatorMetadata") or []
    if offset > 0 and not gms:
        offset = 0
        raw = call_ls_rpc(
            "GetCascadeTrajectoryGeneratorMetadata",
            {"cascadeId": conv_id, "generatorMetadataOffset": 0},
            state_dir=state_dir,
            timeout=timeout,
        )
        gms = (raw or {}).get("generatorMetadata") or []

    if not gms:
        return (prev or {}).get("meta")

    chosen = None
    for item in reversed(gms):
        if isinstance(item, dict) and item.get("chatModel"):
            chosen = item
            break
    if not chosen:
        return (prev or {}).get("meta")

    cm = chosen.get("chatModel") or {}
    cw = ((cm.get("chatStartMetadata") or {}).get("contextWindowMetadata")) or {}
    s_indices = chosen.get("stepIndices") or []
    gen_max_step = max((int(x) for x in s_indices), default=-1) if s_indices else -1
    meta = {
        "model_code": str(cm.get("model") or (cm.get("usage") or {}).get("model") or ""),
        "response_model": str(cm.get("responseModel") or ""),
        "api_provider": str((cm.get("usage") or {}).get("apiProvider") or ""),
        "max_context_tokens": int(cw.get("maxContextTokens") or 0),
        "estimated_tokens_used": int(cw.get("estimatedTokensUsed") or 0),
        "gen_max_step_idx": gen_max_step,
    }
    if cache_file:
        _write_private_json(
            cache_file,
            {
                "ts": now_ts,
                "gm_count": offset + len(gms),
                "last_planner_step": last_planner_step,
                "meta": meta,
            },
        )
    return meta


def resolve_model_and_ceiling(
    *,
    ui_model_label: str = "",
    ui_model_step: int = -1,
    max_total_tokens: int = 0,
    traj_meta: dict[str, Any] | None = None,
    catalog: dict[str, Any] | None = None,
    default_ceiling: int = BASE_CEILING,
) -> dict[str, Any]:
    """Определяет текущую модель, пул квот (`google` / `3p`) и точный порог контекста."""
    gen_step = int((traj_meta or {}).get("gen_max_step_idx", -1))
    pending_new_ui = bool(ui_model_label) and ui_model_step > gen_step

    by_code = (catalog or {}).get("by_code") or {}
    by_label = (catalog or {}).get("by_label") or {}
    mcode = str((traj_meta or {}).get("model_code") or "")
    by_code_entry = by_code.get(mcode) if mcode else None
    by_ui_entry = by_label.get(ui_model_label.lower()) if ui_model_label else None

    if pending_new_ui:
        model_label = ui_model_label
        model_code = (by_ui_entry or {}).get("code", "")
    else:
        model_label = (
            (by_code_entry or {}).get("label")
            or ui_model_label
            or format_fallback_model_name(
                str((traj_meta or {}).get("response_model") or ""),
                mcode,
            )
            or str((catalog or {}).get("default_label") or "")
        )
        model_code = mcode or (by_ui_entry or {}).get("code", "") or str((catalog or {}).get("default_code") or "")

    pool = "google"
    resp_model = str((traj_meta or {}).get("response_model") or "")
    api_prov = str((traj_meta or {}).get("api_provider") or "")
    if is_third_party_text(f"{model_label} {model_code} {resp_model}"):
        pool = "3p"
    elif not pending_new_ui and (by_code_entry or {}).get("pool"):
        pool = str(by_code_entry["pool"])
    elif (by_ui_entry or {}).get("pool"):
        pool = str(by_ui_entry["pool"])
    elif not pending_new_ui and api_prov:
        pool = "google" if re.search(r"google|gemini", api_prov, re.IGNORECASE) else "3p"

    live_max = int((traj_meta or {}).get("max_context_tokens") or 0)
    if not pending_new_ui and live_max > 0:
        ceiling = live_max
    else:
        ceiling = fallback_ceiling_by_model(
            model_label,
            model_code,
            max_total=max_total_tokens,
            default_ceiling=default_ceiling,
        )

    return {
        "model_label": model_label,
        "model_code": model_code,
        "model_pool": pool,
        "pool_name": "Claude/GPT" if pool == "3p" else "Gemini",
        "provider_badge": "Сторонняя" if pool == "3p" else "Google",
        "ceiling": ceiling,
    }


def compute_compact_thresholds(ceiling: int, cfg: dict[str, Any]) -> tuple[int, int]:
    """Возвращает `(warn_tokens, suggest_tokens)` соразмерно потолку активной модели."""
    env_warn = os.environ.get("AGY_SOFT_WARN_TOKENS")
    if env_warn:
        warn_tok = int(env_warn)
    elif ceiling >= 450000:
        warn_tok = round(ceiling * 0.80)
    elif ceiling < 200000:
        warn_tok = round(ceiling * 0.78)
    else:
        warn_tok = int(cfg.get("soft_compact_warn_tokens", 200000))

    if ceiling >= 450000:
        suggest_tok = round(ceiling * 0.72)
    elif ceiling < 200000:
        suggest_tok = round(ceiling * 0.70)
    else:
        suggest_tok = int(cfg.get("stop_compact_suggest_tokens", 180000))
    return warn_tok, suggest_tok
