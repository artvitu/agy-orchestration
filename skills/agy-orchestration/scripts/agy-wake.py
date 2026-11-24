#!/usr/bin/env python3
"""Пробуждение, реестр ролей и создание сессий Google Antigravity Desktop через локальный RPC.

Использует внутренний `LanguageServerService` запущенного `Antigravity Desktop`
(обнаружение порта и CSRF-токена через `~/.gemini/antigravity/daemon/hub_*.json`
в `model_context.py`). Не имеет внешних зависимостей за пределами стандартной библиотеки Python 3.10+.

Возможности (проверено вживую на Antigravity Desktop 2.34.0, 24.11.2026):
  - list / find / status: просмотр корневых сессий левой боковой панели (по умолчанию
    скрывает дочерних субагентов с `parentConversationId`, флаг `--include-subagents`);
  - registry / bind / unbind / discover: сопоставление `<роль> <-> <cascadeId>`
    в JSON-реестре с атомарной записью и автопоиском ролей по маркерам `[role:<slug>]`,
    `/resume-chat <slug>`, префиксу заголовка `[<slug>] ...` или `--from-titles`;
  - send: пробуждение спящей (`IDLE`) сессии или постановка в очередь (`WHEN_IDLE`, `--queue`)
    по роли/UUID/заголовку без сброса выбранной модели, с чтением `--body-file`,
    анти-петлёй `--cooldown` и защитой резерва подписочной квоты (`>10%`);
  - spawn / preset: создание сессии сразу внутри проекта (`projectEnvConfig`),
    опциональная автопривязка `--slug` и установка режима `AGENT_PERMISSION_PRESET_TURBO`
    (`--preset turbo` нужен, чтобы сессия сама могла вызывать локальный RPC на `127.0.0.1`
    без блокировки сетевой песочницей macOS `Operation not permitted`);
  - delete: безопасное удаление отладочной `IDLE`-сессии строго по полному 36-символьному UUID.

Без `--yes` изменяющие команды (`send`, `spawn`, `bind`, `unbind`, `preset`, `delete`,
`discover --sync`) работают в безопасном режиме сухого прогона (`dry-run`).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import model_context as mc  # noqa: E402


DEFAULT_STATE_DIR = Path.home() / ".gemini/antigravity/cache/agy-wake"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
SLUG_MARKER_RE = re.compile(
    r"(?:/(?:resume-chat|new-chat)\s+|\[role:|@role:)([a-z0-9][a-z0-9_-]{0,62})",
    re.IGNORECASE,
)
TITLE_BRACKET_SLUG_RE = re.compile(r"^\s*\[([a-z0-9][a-z0-9_-]{0,47})\]\s+", re.IGNORECASE)
QUOTA_MIN_FRACTION = 0.10
DEFAULT_COOLDOWN_SEC = 60

PRESET_MAP = {
    "auto": "AGENT_PERMISSION_PRESET_AUTO",
    "turbo": "AGENT_PERMISSION_PRESET_TURBO",
}

_CYR_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "j", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class WakeError(RuntimeError):
    """Ожидаемая ошибка валидации, поиска или RPC с кодом возврата CLI."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _detect_workspace_root() -> Path:
    env_ws = os.environ.get("WORKSPACE_ROOT", "").strip()
    if env_ws:
        return Path(env_ws).expanduser().resolve()
    cur = Path.cwd().resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists() or (candidate / ".agents").is_dir():
            return candidate
    return cur


def _state_dir() -> Path:
    env_p = os.environ.get("AGY_WAKE_STATE_DIR", "").strip()
    return Path(env_p).expanduser().resolve() if env_p else DEFAULT_STATE_DIR


def _brain_dir() -> Path:
    env_p = os.environ.get("AGY_BRAIN_DIR", "").strip()
    return Path(env_p).expanduser().resolve() if env_p else (Path.home() / ".gemini/antigravity/brain")


def _registry_path(explicit: str = "") -> Path:
    """Выбирает путь к реестру сессий без загрязнения чужого git-репозитория."""
    if (explicit or "").strip():
        return Path(explicit).expanduser().resolve()
    env_p = os.environ.get("AGY_SESSIONS_FILE", "").strip()
    if env_p:
        return Path(env_p).expanduser().resolve()
    sibling = SCRIPT_DIR / "agy-sessions.json"
    if sibling.is_file():
        return sibling
    ws_root = _detect_workspace_root()
    ws_reg = ws_root / ".agents/agy-sessions.json"
    if ws_reg.is_file():
        return ws_reg
    return _state_dir() / f"sessions-{mc.safe_id(ws_root.name or 'default')}.json"


def _load_registry(path: Path) -> dict[str, Any]:
    ws_root = _detect_workspace_root()
    if not path.is_file():
        return {
            "schema_version": 1,
            "platform": "Google Antigravity Desktop 2.34+",
            "project": ws_root.name or "default",
            "roles": {},
        }
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("корень реестра должен быть JSON-объектом")
        doc.setdefault("schema_version", 1)
        doc.setdefault("roles", {})
        return doc
    except Exception as exc:
        raise WakeError(f"Не удалось прочитать реестр {path}: {exc}", exit_code=2)


def _save_registry(path: Path, doc: dict[str, Any]) -> None:
    """Атомарно сохраняет JSON-реестр через временный файл в той же папке."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".agy-sessions-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        try:
            if str(path).startswith(str(_state_dir())):
                os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _slugify_title(title: str) -> str:
    low = (title or "").strip().lower()
    out_chars: list[str] = []
    for ch in low:
        if ch in _CYR_MAP:
            out_chars.append(_CYR_MAP[ch])
        elif ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out_chars.append(ch)
        else:
            out_chars.append("-")
    slug = re.sub(r"-+", "-", "".join(out_chars)).strip("-")
    slug = slug[:40].rstrip("-")
    if slug and not ("a" <= slug[0] <= "z" or "0" <= slug[0] <= "9"):
        slug = f"s-{slug}"
    return slug


def _read_message_input(text_arg: str | None, body_file_arg: str | None) -> str:
    if body_file_arg:
        bf = body_file_arg.strip()
        if bf == "-":
            raw = sys.stdin.read()
        else:
            p = Path(bf).expanduser()
            if not p.is_file():
                raise WakeError(f"Файл сообщения не найден: {p}", exit_code=2)
            raw = p.read_text(encoding="utf-8")
        if text_arg and text_arg.strip():
            raise WakeError("Укажи либо `--text`, либо `--body-file`, но не оба сразу.", exit_code=2)
        return raw
    if text_arg is not None:
        return text_arg
    raise WakeError("Не передан текст сообщения: укажи `--text \"...\"` или `--body-file <файл>`.", exit_code=2)


def _rpc(method: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    code, body = mc.call_ls_rpc_strict(method, payload, state_dir=None, timeout=timeout)
    if code == 200:
        return body if isinstance(body, dict) else {"result": body}
    if code == 0:
        raise WakeError(f"Нет связи с Antigravity Language Server: {body}", exit_code=2)
    msg = body.get("message") if isinstance(body, dict) else str(body)
    raise WakeError(f"Ошибка RPC {method} (HTTP {code}): {msg}", exit_code=1)


def _fetch_projects() -> dict[str, dict[str, Any]]:
    ids = _rpc("ListProjects", {}).get("projectIds") or []
    if not ids:
        return {}
    projects = _rpc("ReadProjects", {"ids": ids}).get("projects") or []
    return {str(p["id"]).strip(): p for p in projects if str(p.get("id") or "").strip()}


def _fetch_trajectories() -> dict[str, dict[str, Any]]:
    return _rpc("GetAllCascadeTrajectories", {}).get("trajectorySummaries") or {}


def _extract_session_row(
    cid: str,
    info: dict[str, Any],
    projects: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    meta = info.get("trajectoryMetadata") or {}
    ann = info.get("annotations") or {}
    pid = str(meta.get("projectId") or "").strip()
    proj_name = str((projects.get(pid) or {}).get("name") or ("Conversations" if not pid else pid))
    title = str(ann.get("title") or info.get("summary") or "(без названия)").strip()
    raw_status = str(info.get("status") or "").strip()
    short_status = raw_status.removeprefix("CASCADE_RUN_STATUS_") or raw_status
    parent_cid = str(meta.get("parentConversationId") or "").strip()
    workspaces = [
        str(ws.get("workspaceFolderAbsoluteUri") or "").removeprefix("file://")
        for ws in (info.get("workspaces") or meta.get("workspaces") or [])
        if isinstance(ws, dict) and ws.get("workspaceFolderAbsoluteUri")
    ]
    return {
        "cascade_id": cid,
        "short_id": cid[:8],
        "title": title,
        "status": short_status,
        "raw_status": raw_status,
        "project_id": pid,
        "project_name": proj_name,
        "parent_conversation_id": parent_cid,
        "is_subagent": bool(parent_cid),
        "killed": bool(info.get("killed", False)),
        "permission_preset": str(ann.get("permissionPreset") or ""),
        "step_count": int(info.get("stepCount") or 0),
        "last_modified": str(info.get("lastModifiedTime") or ""),
        "created_time": str(info.get("createdTime") or ""),
        "workspaces": workspaces,
    }


def _match_project_filter(row: dict[str, Any], project_query: str) -> bool:
    if not project_query:
        return True
    q = project_query.strip().lower()
    return q == row["project_id"].lower() or q in row["project_name"].lower()


def _auto_project_query(projects: dict[str, dict[str, Any]]) -> str:
    """Подбирает имя проекта в Antigravity по текущему рабочему каталогу."""
    ws_name = _detect_workspace_root().name.lower()
    real_projects = [
        (pid, str(p.get("name") or "").strip())
        for pid, p in projects.items()
        if pid != "outside-of-project" and str(p.get("name") or "").strip().lower() != "outside of project"
    ]
    for pid, name in real_projects:
        if name.lower() == ws_name:
            return name
    if len(real_projects) == 1:
        return real_projects[0][1] or real_projects[0][0]
    return ws_name


def _resolve_project(project_query: str, projects: dict[str, dict[str, Any]]) -> tuple[str, str]:
    q = project_query.strip()
    if not q:
        raise WakeError("Не указан проект (--project).")
    if q in projects:
        return q, str(projects[q].get("name") or q)
    exact = [
        (pid, str(p.get("name") or pid))
        for pid, p in projects.items()
        if str(p.get("name") or "").strip().lower() == q.lower()
    ]
    if len(exact) == 1:
        return exact[0]
    partial = [
        (pid, str(p.get("name") or pid))
        for pid, p in projects.items()
        if q.lower() in str(p.get("name") or "").lower()
    ]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        names = ", ".join(f"{p.get('name')} ({pid[:8]})" for pid, p in projects.items())
        raise WakeError(f"Проект {q!r} не найден. Доступны: {names}")
    names = ", ".join(f"{name} ({pid[:8]})" for pid, name in partial)
    raise WakeError(f"Найдено несколько проектов по запросу {q!r}: {names}")


def _resolve_session(
    target: str,
    rows: list[dict[str, Any]],
    *,
    project_filter: str = "",
    registry: dict[str, Any] | None = None,
    include_subagents: bool = False,
) -> tuple[dict[str, Any], str]:
    """Ищет сессию по роли из реестра, UUID, префиксу UUID или заголовку.

    Возвращает `(row, matched_slug)`.
    """
    q = (target or "").strip()
    if not q:
        raise WakeError("Не указан адресат (--to).")

    roles = (registry or {}).get("roles") or {}
    if q in roles:
        rec = roles[q] if isinstance(roles[q], dict) else {}
        reg_cid = str(rec.get("cascade_id") or "").strip()
        if not reg_cid:
            raise WakeError(
                f"Роль `{q}` отвязана в реестре (`cascade_id` пуст). Выполни `bind` или `spawn`.",
                exit_code=2,
            )
        by_cid = {r["cascade_id"].lower(): r for r in rows}
        found = by_cid.get(reg_cid.lower())
        if not found or found.get("killed"):
            raise WakeError(
                f"Роль `{q}` привязана к `{reg_cid[:8]}`, но эта сессия не найдена в Antigravity "
                "(закрыта или удалена). Перепривяжи через `bind`.",
                exit_code=2,
            )
        return found, q

    ql = q.lower()
    for r in rows:
        if r["cascade_id"].lower() == ql and not r.get("killed"):
            return r, ""

    pool = [r for r in rows if not r.get("killed")]
    if not include_subagents:
        pool = [r for r in pool if not r.get("is_subagent")]
    scoped = [r for r in pool if _match_project_filter(r, project_filter)] if project_filter else pool

    prefix = [r for r in scoped if r["cascade_id"].lower().startswith(ql)] if len(ql) >= 6 else []
    if len(prefix) == 1:
        return prefix[0], ""

    exact_title = [r for r in scoped if r["title"].lower() == ql]
    if len(exact_title) == 1:
        return exact_title[0], ""

    substr = [
        r
        for r in scoped
        if ql in r["title"].lower() or ql in r["cascade_id"].lower()
    ]
    if len(substr) == 1:
        return substr[0], ""
    if not substr:
        raise WakeError(f"Сессия по запросу {q!r} не найдена.")
    preview = "; ".join(f"{r['short_id']} [{r['project_name']}] {r['title']}" for r in substr[:8])
    raise WakeError(f"Запрос {q!r} неоднозначен ({len(substr)} совп.): {preview}")


def _resolve_model(model_query: str) -> dict[str, str]:
    q = (model_query or "").strip()
    catalog = mc.fetch_model_catalog(state_dir=None, ttl_sec=0, timeout=5.0)
    by_code = catalog.get("by_code") or {}
    by_label = catalog.get("by_label") or {}
    if not by_code and not by_label:
        if q.startswith("MODEL_"):
            pool = "3p" if mc.is_third_party_text(q) else "google"
            return {"code": q, "label": q, "pool": pool}
        raise WakeError("Не удалось получить каталог моделей (GetUserStatus).")

    if q in by_code:
        return by_code[q]
    ql = q.lower()
    if ql in by_label:
        return by_label[ql]

    matches = [
        entry
        for entry in by_code.values()
        if ql in entry.get("label", "").lower() or ql in entry.get("code", "").lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        avail = ", ".join(sorted(e.get("label", "") for e in by_code.values() if e.get("label"))[:12])
        raise WakeError(f"Модель {q!r} не найдена. Примеры: {avail}")
    cand = ", ".join(f"{m['label']} ({m['code']})" for m in matches[:8])
    raise WakeError(f"Найдено несколько моделей по запросу {q!r}: {cand}")


def _session_model_info(cid: str) -> dict[str, str]:
    catalog = mc.fetch_model_catalog(state_dir=None, ttl_sec=300, timeout=5.0)
    meta = mc.fetch_trajectory_meta(cid, state_dir=None, ttl_sec=0, timeout=5.0)
    info = mc.resolve_model_and_ceiling(traj_meta=meta, catalog=catalog)
    return {
        "code": str(info.get("model_code") or ""),
        "label": str(info.get("model_label") or ""),
        "pool": str(info.get("model_pool") or "google"),
    }


def _check_quota_reserve(pool: str) -> tuple[bool, list[str]]:
    """Проверяет резерв квоты (> 10%) в нужном пуле (`google` / `3p`)."""
    raw = mc.call_ls_rpc("RetrieveUserQuotaSummary", {}, state_dir=None, timeout=5.0)
    if not raw:
        return True, []
    prefix = "3p-" if pool == "3p" else "gemini-"
    low: list[str] = []
    for grp in (raw.get("response") or {}).get("groups") or []:
        for b in grp.get("buckets") or []:
            bid = str(b.get("bucketId") or "")
            if not bid.startswith(prefix):
                continue
            frac = float(b.get("remainingFraction", 1.0))
            if frac <= QUOTA_MIN_FRACTION:
                low.append(f"{bid}={frac * 100:.1f}% (reset {b.get('resetTime', '')})")
    return len(low) == 0, low


def _cooldown_file(sender: str, target_cid: str) -> Path:
    s_key = mc.safe_id(sender or "anon")
    t_key = mc.safe_id(target_cid)
    return _state_dir() / f"cd__{s_key}__{t_key}.json"


def _check_cooldown(sender: str, target_cid: str, cooldown_sec: int) -> tuple[bool, float]:
    if not sender or cooldown_sec <= 0:
        return True, 0.0
    path = _cooldown_file(sender, target_cid)
    if not path.is_file():
        return True, 0.0
    try:
        ts = float(json.loads(path.read_text(encoding="utf-8")).get("ts") or 0)
    except Exception:
        return True, 0.0
    elapsed = time.time() - ts
    if elapsed < cooldown_sec:
        return False, cooldown_sec - elapsed
    return True, 0.0


def _mark_cooldown(sender: str, target_cid: str) -> None:
    if not sender:
        return
    mc._write_private_json(_cooldown_file(sender, target_cid), {"ts": time.time(), "sender": sender, "target": target_cid})


def _format_signal_text(text: str, sender: str) -> str:
    body = text.strip()
    if not body:
        raise WakeError("Текст сообщения не может быть пустым.")
    s = (sender or "").strip()
    if not s:
        return body
    low = body.lower()
    if low.startswith(f"от {s.lower()}") or low.startswith(f"[от {s.lower()}"):
        return body
    return f"от {s}: {body}"


def _extract_last_planner_reply(cid: str) -> dict[str, Any]:
    traj = _rpc("GetCascadeTrajectory", {"cascadeId": cid}, timeout=10.0)
    steps = (traj.get("trajectory") or {}).get("steps") or []
    for idx in range(len(steps) - 1, -1, -1):
        step = steps[idx]
        if step.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
            pr = step.get("plannerResponse") or {}
            return {
                "step_index": idx,
                "status": step.get("status"),
                "response": str(pr.get("response") or ""),
                "total_steps": len(steps),
            }
    return {"step_index": -1, "status": "", "response": "", "total_steps": len(steps)}


def _wait_for_reply(cid: str, prev_steps: int, wait_sec: int, *, was_running: bool = False) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, wait_sec)
    saw_progress = False
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(1.0)
        sums = _fetch_trajectories()
        info = sums.get(cid) or {}
        st = str(info.get("status") or "")
        cur_steps = int(info.get("stepCount") or 0)
        last_status = st
        if cur_steps > prev_steps or (not was_running and st == "CASCADE_RUN_STATUS_RUNNING"):
            saw_progress = True
        if saw_progress and st == "CASCADE_RUN_STATUS_IDLE" and (not was_running or cur_steps > prev_steps):
            reply = _extract_last_planner_reply(cid)
            reply["final_status"] = "IDLE"
            reply["waited"] = True
            return reply
    return {
        "final_status": last_status.removeprefix("CASCADE_RUN_STATUS_") or "TIMEOUT",
        "waited": True,
        "timeout": True,
    }


def _scan_slug_from_transcript(cid: str) -> tuple[str, str]:
    tp = _brain_dir() / cid / ".system_generated/logs/transcript.jsonl"
    if not tp.is_file():
        return "", ""
    try:
        with open(tp, encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh):
                if idx > 120:
                    break
                m = SLUG_MARKER_RE.search(line)
                if m:
                    return m.group(1).lower(), "Автопоиск по маркеру в транскрипте"
    except OSError:
        return "", ""
    return "", ""


def _scan_slug_for_session(row: dict[str, Any], *, from_titles: bool = False) -> tuple[str, str]:
    slug, note = _scan_slug_from_transcript(row["cascade_id"])
    if slug:
        return slug, note
    m_title = TITLE_BRACKET_SLUG_RE.match(row.get("title") or "")
    if m_title:
        return m_title.group(1).lower(), "Автопоиск по префиксу заголовка [role]"
    if from_titles:
        title_raw = row.get("title") or ""
        if title_raw and title_raw != "(без названия)":
            gen = _slugify_title(title_raw)
            if gen:
                return gen, "Автопоиск по заголовку (--from-titles)"
    return "", ""


def _emit(data: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_list(args: argparse.Namespace) -> int:
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    rows = [_extract_session_row(cid, info, projects) for cid, info in sums.items() if not info.get("killed")]
    if not args.include_subagents:
        rows = [r for r in rows if not r["is_subagent"]]
    if args.project:
        rows = [r for r in rows if _match_project_filter(r, args.project)]
    if args.status:
        want = args.status.strip().upper()
        rows = [r for r in rows if r["status"].upper() == want]
    rows.sort(key=lambda r: r["last_modified"], reverse=True)
    if args.limit > 0:
        rows = rows[: args.limit]
    if args.json:
        _emit({"count": len(rows), "sessions": rows}, as_json=True)
        return 0
    for r in rows:
        sub_mark = " [sub]" if r["is_subagent"] else ""
        print(f"{r['short_id']} | {r['status']:7s} | {r['project_name']:14s} | {r['step_count']:4d}ш | {r['title']}{sub_mark}")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    q = args.query.strip().lower()
    rows = [_extract_session_row(cid, info, projects) for cid, info in sums.items() if not info.get("killed")]
    if not args.include_subagents:
        rows = [r for r in rows if not r["is_subagent"]]
    matched = [
        r for r in rows
        if (not args.project or _match_project_filter(r, args.project))
        and (q in r["title"].lower() or q in r["cascade_id"].lower())
    ]
    matched.sort(key=lambda r: r["last_modified"], reverse=True)
    if args.json:
        _emit({"count": len(matched), "sessions": matched}, as_json=True)
        return 0
    for r in matched:
        print(f"{r['cascade_id']} | {r['status']:7s} | {r['project_name']:14s} | {r['title']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    reg_path = _registry_path(args.registry)
    reg = _load_registry(reg_path)
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    rows = [_extract_session_row(cid, info, projects) for cid, info in sums.items()]
    row, matched_slug = _resolve_session(args.to, rows, project_filter=args.project or "", registry=reg)
    model = _session_model_info(row["cascade_id"])
    last_reply = _extract_last_planner_reply(row["cascade_id"])
    out = {**row, "slug": matched_slug, "model": model, "last_reply_preview": last_reply.get("response", "")[:300]}
    if args.json:
        _emit(out, as_json=True)
        return 0
    print(f"ID:      {out['cascade_id']}")
    if matched_slug:
        print(f"Роль:    {matched_slug}")
    print(f"Заголовок: {out['title']}")
    print(f"Проект:  {out['project_name']} ({out['project_id'] or 'Conversations'})")
    print(f"Статус:  {out['status']} (шагов: {out['step_count']}, пресет: {out['permission_preset'] or 'default'})")
    print(f"Модель:  {model['label'] or '-'} ({model['code'] or '-'}, пул {model['pool']})")
    if out["last_reply_preview"]:
        print(f"Ответ:   {out['last_reply_preview']}")
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    reg_path = _registry_path(args.registry)
    reg = _load_registry(reg_path)
    roles = reg.get("roles") or {}
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    rows_out: list[dict[str, Any]] = []
    for slug in sorted(roles):
        rec = roles[slug] if isinstance(roles[slug], dict) else {}
        cid = str(rec.get("cascade_id") or "").strip()
        info = sums.get(cid)
        if not cid:
            st = "UNBOUND"
        elif not info or info.get("killed"):
            st = "MISSING"
        else:
            st = str(info.get("status") or "").removeprefix("CASCADE_RUN_STATUS_") or "UNKNOWN"
        row = _extract_session_row(cid, info, projects) if info else {}
        rows_out.append({
            "slug": slug,
            "cascade_id": cid,
            "status": st,
            "title": row.get("title") or rec.get("title") or "",
            "project_name": row.get("project_name") or "",
            "permission_preset": row.get("permission_preset") or "",
            "note": rec.get("note") or "",
        })
    if args.json:
        _emit({"registry_path": str(reg_path), "count": len(rows_out), "roles": rows_out}, as_json=True)
        return 0
    if not rows_out:
        print(f"Реестр {reg_path} пока пуст (выполни `discover --sync --yes` или `bind`).")
        return 0
    for r in rows_out:
        sid = r["cascade_id"][:8] if r["cascade_id"] else "-"
        print(f"{r['slug']:30s} | {sid:8s} | {r['status']:8s} | {r['title']}")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    reg_path = _registry_path(args.registry)
    reg = _load_registry(reg_path)
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    rows = [_extract_session_row(cid, info, projects) for cid, info in sums.items()]
    row, _ = _resolve_session(args.to, rows, project_filter=args.project or "", registry=None)
    if row["is_subagent"]:
        raise WakeError("Нельзя привязать дочернего субагента в реестр постоянных сессий.", exit_code=2)
    slug = args.slug.strip()
    if not slug:
        raise WakeError("Роль (--slug) не может быть пустой.")
    entry = {
        "cascade_id": row["cascade_id"],
        "title": row["title"],
        "project_id": row["project_id"],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if args.note:
        entry["note"] = args.note.strip()
    if not args.yes:
        if args.json:
            _emit({"dry_run": True, "action": "bind", "slug": slug, "entry": entry, "registry_path": str(reg_path)}, as_json=True)
        else:
            print(f"[DRY-RUN] Привязка `{slug}` -> {row['cascade_id']} ({row['title']}) в {reg_path}. Добавь `--yes`.")
        return 0
    reg.setdefault("roles", {})[slug] = entry
    _save_registry(reg_path, reg)
    if args.json:
        _emit({"bound": True, "slug": slug, "entry": entry, "registry_path": str(reg_path)}, as_json=True)
    else:
        print(f"Привязано `{slug}` -> {row['short_id']} ({row['title']}).")
    return 0


def cmd_unbind(args: argparse.Namespace) -> int:
    reg_path = _registry_path(args.registry)
    reg = _load_registry(reg_path)
    slug = args.slug.strip()
    roles = reg.get("roles") or {}
    if slug not in roles:
        raise WakeError(f"Роль `{slug}` отсутствует в {reg_path}.")
    if not args.yes:
        if args.json:
            _emit({"dry_run": True, "action": "unbind", "slug": slug}, as_json=True)
        else:
            print(f"[DRY-RUN] Снятие привязки `{slug}` в {reg_path}. Добавь `--yes`.")
        return 0
    rec = dict(roles[slug]) if isinstance(roles[slug], dict) else {}
    prev_cid = str(rec.get("cascade_id") or "")
    rec["cascade_id"] = ""
    rec["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["note"] = args.note.strip() if args.note else f"[отвязана прежняя сессия {prev_cid[:8]}]"
    roles[slug] = rec
    _save_registry(reg_path, reg)
    if args.json:
        _emit({"unbound": True, "slug": slug, "previous_cascade_id": prev_cid}, as_json=True)
    else:
        print(f"Отвязана роль `{slug}` (прежняя сессия: {prev_cid[:8]}).")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    reg_path = _registry_path(args.registry)
    reg = _load_registry(reg_path)
    roles = reg.setdefault("roles", {})
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    rows = [_extract_session_row(cid, info, projects) for cid, info in sums.items() if not info.get("killed")]
    rows = [r for r in rows if not r["is_subagent"]]

    proj_filter = args.project
    if not proj_filter and not args.all_projects:
        cand = _auto_project_query(projects)
        if any(_match_project_filter(r, cand) for r in rows):
            proj_filter = cand
    if proj_filter:
        rows = [r for r in rows if _match_project_filter(r, proj_filter)]
    rows.sort(key=lambda r: r["last_modified"], reverse=True)

    discovered: dict[str, dict[str, Any]] = {}
    for r in rows:
        base_slug, note = _scan_slug_for_session(r, from_titles=bool(args.from_titles))
        if not base_slug:
            continue
        slug = base_slug
        if slug in discovered:
            if not args.from_titles:
                continue
            idx = 2
            while f"{base_slug}-{idx}" in discovered:
                idx += 1
            slug = f"{base_slug}-{idx}"
        discovered[slug] = {
            "cascade_id": r["cascade_id"],
            "title": r["title"],
            "project_id": r["project_id"],
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": note,
        }

    added: list[str] = []
    updated: list[str] = []
    for slug, entry in sorted(discovered.items()):
        cur = roles.get(slug)
        if not cur or not str((cur or {}).get("cascade_id") or "").strip():
            added.append(slug)
            if args.sync and args.yes:
                roles[slug] = entry
        elif args.force and cur.get("cascade_id") != entry["cascade_id"]:
            updated.append(slug)
            if args.sync and args.yes:
                roles[slug] = entry

    if args.sync and args.yes and (added or updated):
        _save_registry(reg_path, reg)

    out = {
        "registry_path": str(reg_path),
        "project_filter": proj_filter,
        "discovered_count": len(discovered),
        "added": added,
        "updated": updated,
        "synced": bool(args.sync and args.yes),
        "sessions": discovered,
    }
    if args.json:
        _emit(out, as_json=True)
        return 0
    for slug, e in sorted(discovered.items()):
        mark = "+" if slug in added else ("~" if slug in updated else "=")
        print(f"{mark} {slug:30s} -> {e['cascade_id'][:8]} | {e['title']}")
    if not discovered:
        print("Подходящих сессий не найдено (передай `--from-titles`, чтобы построить роли из заголовков вкладок).")
    elif args.sync and not args.yes and (added or updated):
        print(f"[DRY-RUN] Для записи в {reg_path} добавь `--yes`.")
    return 0


def cmd_preset(args: argparse.Namespace) -> int:
    reg = _load_registry(_registry_path(args.registry))
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    rows = [_extract_session_row(cid, info, projects) for cid, info in sums.items()]
    row, _ = _resolve_session(args.to, rows, project_filter=args.project or "", registry=reg)
    preset_enum = PRESET_MAP[args.preset]
    if not args.yes:
        if args.json:
            _emit({"dry_run": True, "action": "preset", "cascade_id": row["cascade_id"], "permissionPreset": preset_enum}, as_json=True)
        else:
            print(f"[DRY-RUN] Смена пресета {row['short_id']} ({row['title']}) -> {preset_enum}. Добавь `--yes`.")
        return 0
    _rpc("UpdateConversationAnnotations", {
        "cascadeId": row["cascade_id"],
        "annotations": {"permissionPreset": preset_enum},
        "mergeAnnotations": True,
    })
    if args.json:
        _emit({"updated": True, "cascade_id": row["cascade_id"], "permissionPreset": preset_enum}, as_json=True)
    else:
        print(f"Пресет сессии {row['short_id']} ({row['title']}) установлен в {preset_enum}.")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    raw_text = _read_message_input(args.text, args.body_file)
    reg = _load_registry(_registry_path(args.registry))
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    rows = [_extract_session_row(cid, info, projects) for cid, info in sums.items()]
    row, matched_slug = _resolve_session(args.to, rows, project_filter=args.project or "", registry=reg)
    cid = row["cascade_id"]
    sender = (args.sender or "").strip()

    if sender and not args.allow_self:
        if sender == matched_slug or sender.lower() == cid.lower():
            raise WakeError(
                f"Запрещена отправка самому себе (`{sender}` -> `{row['short_id']}`). "
                "Для явной проверки передай `--allow-self`.",
                exit_code=2,
            )

    cur_model = _session_model_info(cid)
    override_model = _resolve_model(args.model) if args.model else None
    effective_pool = (override_model or cur_model).get("pool") or "google"
    msg_text = _format_signal_text(raw_text, sender)
    was_running = row["status"] == "RUNNING"

    if was_running and not args.queue:
        raise WakeError(
            f"Сессия {row['short_id']} ({row['title']}) сейчас RUNNING. "
            "Дождись IDLE или передай `--queue` (`MESSAGE_DELIVERY_STRATEGY_WHEN_IDLE`).",
            exit_code=2,
        )

    payload: dict[str, Any] = {
        "cascadeId": cid,
        "items": [{"text": msg_text}],
    }
    if args.queue:
        payload["deliveryStrategy"] = "MESSAGE_DELIVERY_STRATEGY_WHEN_IDLE"
    if override_model:
        payload["cascadeConfig"] = {
            "plannerConfig": {"requestedModel": {"model": override_model["code"]}}
        }

    if not args.yes:
        preview = {
            "dry_run": True,
            "action": "send",
            "target": row,
            "matched_slug": matched_slug,
            "current_model": cur_model,
            "override_model": override_model,
            "payload": payload,
        }
        if args.json:
            _emit(preview, as_json=True)
        else:
            print(f"[DRY-RUN] Отправка в {row['short_id']} [{row['project_name']}] {row['title']} ({row['status']})")
            print(f"Модель: {(override_model or cur_model).get('label') or 'текущая сессии'}")
            print(f"Текст:  {msg_text}")
            print("Для реальной отправки добавь флаг `--yes`.")
        return 0

    cd_sec = 0 if args.no_cooldown else int(args.cooldown)
    ok_cd, rem_cd = _check_cooldown(sender, cid, cd_sec)
    if not ok_cd:
        raise WakeError(
            f"Анти-петля (кулдаун {cd_sec}с): `{sender}` уже будил {row['short_id']} менее минуты назад "
            f"(осталось {rem_cd:.1f}с). Для ручного обхода передай `--no-cooldown`.",
            exit_code=2,
        )

    if not args.ignore_budget:
        ok, low = _check_quota_reserve(effective_pool)
        if not ok:
            raise WakeError(
                f"Остановлено правилом резерва квоты (<=10%) для пула {effective_pool}: {', '.join(low)}. "
                "Для явного обхода передай `--ignore-budget`.",
                exit_code=2,
            )

    prev_steps = row["step_count"]
    _rpc("SendUserCascadeMessage", payload, timeout=15.0)
    _mark_cooldown(sender, cid)
    result: dict[str, Any] = {
        "sent": True,
        "cascade_id": cid,
        "matched_slug": matched_slug,
        "title": row["title"],
        "project_name": row["project_name"],
        "queued": bool(args.queue and was_running),
    }
    if args.wait > 0:
        result["wait_result"] = _wait_for_reply(cid, prev_steps, args.wait, was_running=was_running)
    if args.json:
        _emit(result, as_json=True)
    else:
        print(f"Отправлено в {row['short_id']} ({row['title']}).")
        if args.wait > 0:
            wr = result["wait_result"]
            print(f"Итог ожидания: {wr.get('final_status')} | ответ: {wr.get('response', '')}")
    return 0


def cmd_spawn(args: argparse.Namespace) -> int:
    raw_text = _read_message_input(args.text, args.body_file)
    projects = _fetch_projects()
    start_payload: dict[str, Any] = {
        "source": "CORTEX_TRAJECTORY_SOURCE_INTERACTIVE_CASCADE",
    }
    target_project_id = ""
    target_project_name = "Conversations"
    if args.workspace:
        ws_path = Path(args.workspace).expanduser().resolve()
        start_payload["workspaceUris"] = [ws_path.as_uri()]
        target_project_name = f"workspace:{ws_path.name}"
    else:
        proj_query = args.project or _auto_project_query(projects)
        pid, pname = _resolve_project(proj_query, projects)
        start_payload["projectEnvConfig"] = {
            "projectId": pid,
            "defaultProjectEnvironment": {},
        }
        target_project_id = pid
        target_project_name = pname

    model_info = _resolve_model(args.model) if args.model else None
    msg_text = _format_signal_text(raw_text, args.sender or "")
    preset_enum = PRESET_MAP[args.preset] if args.preset else ""
    bind_slug = (args.slug or "").strip()

    if not args.yes:
        preview = {
            "dry_run": True,
            "action": "spawn",
            "project_name": target_project_name,
            "title": args.title or "",
            "slug": bind_slug,
            "permissionPreset": preset_enum,
            "model": model_info,
            "start_payload": start_payload,
            "text": msg_text,
        }
        if args.json:
            _emit(preview, as_json=True)
        else:
            print(f"[DRY-RUN] Создание сессии в проекте {target_project_name}")
            if args.title:
                print(f"Заголовок: {args.title}")
            if bind_slug:
                print(f"Роль:      {bind_slug}")
            if preset_enum:
                print(f"Пресет:    {preset_enum}")
            if model_info:
                print(f"Модель:    {model_info['label']} ({model_info['code']})")
            print(f"Текст:     {msg_text}")
            print("Для реального создания добавь флаг `--yes`.")
        return 0

    if not args.ignore_budget:
        pool = (model_info or {}).get("pool") or "google"
        ok, low = _check_quota_reserve(pool)
        if not ok:
            raise WakeError(
                f"Остановлено правилом резерва квоты (<=10%) для пула {pool}: {', '.join(low)}.",
                exit_code=2,
            )

    cid = str(_rpc("StartCascade", start_payload, timeout=15.0).get("cascadeId") or "").strip()
    if not cid:
        raise WakeError("StartCascade не вернул cascadeId.")
    ann: dict[str, Any] = {}
    if args.title:
        ann["title"] = args.title
    if preset_enum:
        ann["permissionPreset"] = preset_enum
    if ann:
        _rpc("UpdateConversationAnnotations", {"cascadeId": cid, "annotations": ann, "mergeAnnotations": True})

    if bind_slug:
        reg_path = _registry_path(args.registry)
        reg = _load_registry(reg_path)
        reg.setdefault("roles", {})[bind_slug] = {
            "cascade_id": cid,
            "title": args.title or bind_slug,
            "project_id": target_project_id,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "Создана через agy-wake.py spawn",
        }
        _save_registry(reg_path, reg)

    send_payload: dict[str, Any] = {"cascadeId": cid, "items": [{"text": msg_text}]}
    if model_info:
        send_payload["cascadeConfig"] = {
            "plannerConfig": {"requestedModel": {"model": model_info["code"]}}
        }
    _rpc("SendUserCascadeMessage", send_payload, timeout=15.0)
    result: dict[str, Any] = {
        "spawned": True,
        "cascade_id": cid,
        "project_name": target_project_name,
        "title": args.title or "",
        "slug": bind_slug,
        "permissionPreset": preset_enum,
    }
    if args.wait > 0:
        result["wait_result"] = _wait_for_reply(cid, 0, args.wait)
    if args.json:
        _emit(result, as_json=True)
    else:
        print(f"Создана сессия {cid} в {target_project_name}.")
        if bind_slug:
            print(f"Привязана роль `{bind_slug}` -> {cid[:8]}.")
        if args.wait > 0:
            wr = result["wait_result"]
            print(f"Итог ожидания: {wr.get('final_status')} | ответ: {wr.get('response', '')}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    cid = (args.to or "").strip()
    if not UUID_RE.match(cid):
        raise WakeError(
            "Для безопасного удаления `--to` должен быть точным полным UUID (36 символов).",
            exit_code=2,
        )
    projects = _fetch_projects()
    sums = _fetch_trajectories()
    if cid not in sums:
        raise WakeError(f"Сессия {cid} не найдена.")
    row = _extract_session_row(cid, sums[cid], projects)
    if row["status"] == "RUNNING":
        raise WakeError(f"Нельзя удалить работающую (RUNNING) сессию {cid}.", exit_code=2)
    if not args.yes:
        if args.json:
            _emit({"dry_run": True, "action": "delete", "target": row}, as_json=True)
        else:
            print(f"[DRY-RUN] Удаление сессии {cid} [{row['project_name']}] {row['title']}. Добавь `--yes`.")
        return 0
    _rpc("DeleteCascadeTrajectory", {"cascadeId": cid})

    reg_path = _registry_path(args.registry)
    unbound_roles: list[str] = []
    if reg_path.is_file():
        reg = _load_registry(reg_path)
        roles = reg.get("roles") or {}
        for slug, rec in roles.items():
            if isinstance(rec, dict) and str(rec.get("cascade_id") or "").lower() == cid.lower():
                rec["cascade_id"] = ""
                rec["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                rec["note"] = f"[сессия {cid[:8]} удалена через delete]"
                unbound_roles.append(slug)
        if unbound_roles:
            _save_registry(reg_path, reg)

    if args.json:
        _emit({"deleted": True, "cascade_id": cid, "title": row["title"], "unbound_roles": unbound_roles}, as_json=True)
    else:
        print(f"Удалена сессия {cid} ({row['title']}).")
        if unbound_roles:
            print(f"Отвязаны роли в реестре: {', '.join(unbound_roles)}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Управление и пробуждение сессий Google Antigravity Desktop через локальный RPC.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="Список сессий")
    pl.add_argument("--project", default="", help="Фильтр по имени или ID проекта")
    pl.add_argument("--status", default="", help="Фильтр по статусу (IDLE, RUNNING)")
    pl.add_argument("--include-subagents", action="store_true", help="Показывать дочерних субагентов")
    pl.add_argument("--limit", type=int, default=30, help="Сколько последних сессий показать (0 = все)")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pf = sub.add_parser("find", help="Поиск сессий по названию или ID")
    pf.add_argument("query")
    pf.add_argument("--project", default="")
    pf.add_argument("--include-subagents", action="store_true")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_find)

    ps = sub.add_parser("status", help="Подробный статус сессии")
    ps.add_argument("--to", required=True, help="Роль, UUID, префикс или часть заголовка")
    ps.add_argument("--project", default="")
    ps.add_argument("--registry", default="", help="Путь к JSON-реестру ролей")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_status)

    pr = sub.add_parser("registry", help="Показать привязки ролей из реестра")
    pr.add_argument("--registry", default="")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_registry)

    pb = sub.add_parser("bind", help="Привязать имя роли к живой сессии")
    pb.add_argument("--slug", required=True)
    pb.add_argument("--to", required=True, help="UUID или часть заголовка сессии")
    pb.add_argument("--project", default="")
    pb.add_argument("--note", default="")
    pb.add_argument("--registry", default="")
    pb.add_argument("--yes", action="store_true")
    pb.add_argument("--json", action="store_true")
    pb.set_defaults(func=cmd_bind)

    pu = sub.add_parser("unbind", help="Отвязать роль от сессии в реестре")
    pu.add_argument("--slug", required=True)
    pu.add_argument("--note", default="")
    pu.add_argument("--registry", default="")
    pu.add_argument("--yes", action="store_true")
    pu.add_argument("--json", action="store_true")
    pu.set_defaults(func=cmd_unbind)

    pdisc = sub.add_parser("discover", help="Найти роли живых сессий по маркерам или заголовкам")
    pdisc.add_argument("--project", default="", help="Фильтр по проекту (по умолчанию текущий проект)")
    pdisc.add_argument("--all-projects", action="store_true", help="Искать по всем проектам Antigravity")
    pdisc.add_argument("--from-titles", action="store_true", help="Строить имена ролей из заголовков вкладок, если нет маркера [role:...]")
    pdisc.add_argument("--registry", default="")
    pdisc.add_argument("--sync", action="store_true", help="Записать найденные роли в реестр (требует --yes)")
    pdisc.add_argument("--force", action="store_true", help="Перезаписать существующий UUID более свежей сессией")
    pdisc.add_argument("--yes", action="store_true")
    pdisc.add_argument("--json", action="store_true")
    pdisc.set_defaults(func=cmd_discover)

    pp = sub.add_parser("preset", help="Установить режим разрешений сессии (auto или turbo)")
    pp.add_argument("--to", required=True, help="Роль, UUID, префикс или заголовок")
    pp.add_argument("--preset", required=True, choices=tuple(PRESET_MAP.keys()))
    pp.add_argument("--project", default="")
    pp.add_argument("--registry", default="")
    pp.add_argument("--yes", action="store_true")
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(func=cmd_preset)

    p_send = sub.add_parser("send", help="Отправить сообщение / разбудить сессию")
    p_send.add_argument("--to", required=True, help="Роль, UUID, префикс или часть заголовка")
    p_send.add_argument("--text", default=None, help="Текст сообщения (для коротких строк)")
    p_send.add_argument("--body-file", default=None, help="Путь к файлу сообщения (или '-' для stdin)")
    p_send.add_argument("--from", dest="sender", default="", help="Имя роли отправителя для автоподписи 'от <роль>:'")
    p_send.add_argument("--project", default="", help="Уточнить проект при поиске по заголовку")
    p_send.add_argument("--registry", default="", help="Путь к JSON-реестру ролей")
    p_send.add_argument("--model", default="", help="Опционально сменить модель (по умолчанию сохраняется текущая)")
    p_send.add_argument("--queue", action="store_true", help="Поставить в очередь WHEN_IDLE, если сессия RUNNING")
    p_send.add_argument("--wait", type=int, default=0, help="Ждать завершения ответа до N секунд")
    p_send.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_SEC, help="Пауза анти-петли в секундах при --from")
    p_send.add_argument("--no-cooldown", action="store_true", help="Отключить кулдаун для явной серии команд")
    p_send.add_argument("--allow-self", action="store_true", help="Разрешить отправку самому себе")
    p_send.add_argument("--ignore-budget", action="store_true", help="Игнорировать порог 10%% остатка квоты")
    p_send.add_argument("--yes", action="store_true", help="Выполнить отправку (без флага только dry-run)")
    p_send.add_argument("--json", action="store_true")
    p_send.set_defaults(func=cmd_send)

    p_sp = sub.add_parser("spawn", help="Создать новую сессию в проекте и запустить её")
    p_sp.add_argument("--text", default=None, help="Стартовый запрос")
    p_sp.add_argument("--body-file", default=None, help="Путь к файлу стартового запроса (или '-' для stdin)")
    p_sp.add_argument("--title", default="", help="Заголовок сессии")
    p_sp.add_argument("--slug", default="", help="Сразу привязать созданную сессию к роли в реестре")
    p_sp.add_argument("--project", default="", help="Имя или ID проекта (по умолчанию текущий проект)")
    p_sp.add_argument("--workspace", default="", help="Путь к папке вместо привязки к проекту (попадёт в Conversations)")
    p_sp.add_argument("--from", dest="sender", default="", help="Имя роли отправителя")
    p_sp.add_argument("--model", default="", help="Стартовая модель")
    p_sp.add_argument("--preset", default="", choices=("",) + tuple(PRESET_MAP.keys()), help="Пресет разрешений (turbo нужен для исходящих вызовов agy-wake.py)")
    p_sp.add_argument("--registry", default="", help="Путь к JSON-реестру ролей")
    p_sp.add_argument("--wait", type=int, default=0, help="Ждать ответа до N секунд")
    p_sp.add_argument("--ignore-budget", action="store_true")
    p_sp.add_argument("--yes", action="store_true")
    p_sp.add_argument("--json", action="store_true")
    p_sp.set_defaults(func=cmd_spawn)

    pd = sub.add_parser("delete", help="Удалить тестовую IDLE-сессию строго по полному UUID")
    pd.add_argument("--to", required=True, help="Полный 36-символьный UUID")
    pd.add_argument("--registry", default="")
    pd.add_argument("--yes", action="store_true")
    pd.add_argument("--json", action="store_true")
    pd.set_defaults(func=cmd_delete)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WakeError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "exit_code": exc.exit_code}, ensure_ascii=False))
        else:
            print(f"ОШИБКА: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
