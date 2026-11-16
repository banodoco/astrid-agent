"""Plan 55 native-flush trace readers; missing or incomplete evidence is unknown.

Linux input is strace -ff -ttt -T -e trace=process,fsync,fdatasync.
Windows input is tracerpt XML from the FileIO/FileIOInit WPR profile. FileIo
Flush (73) is paired with OpEnd (76): an entry event alone is NOT a completed
flush. Provider identities and event semantics follow Microsoft's FileIo docs.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

FILE_IO = "90cbdc39-4a3e-11d1-84f4-0000f80464e3"
THREAD = "3d6fa8d1-fe05-11d0-9dda-00c04fd7ba7c"
PROCESS = "3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c"


def seconds_ns(value: str) -> int:
    return int(Decimal(value) * 1_000_000_000)


def integer(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    try:
        return int(value, 16 if value.lower().startswith("0x") else 10)
    except ValueError:
        return None


def trace_strace(directory: Path) -> dict:
    records, relations, gaps = [], [], []
    files = sorted(directory.glob("trace.*"))
    if not files:
        return {"events": [], "relations": [], "gaps": ["no strace files"]}
    decoded_files = 0
    for file in files:
        tid = integer(file.suffix[1:])
        if tid is None:
            continue
        decoded_files += 1
        pending = None
        pending_process = None
        terminal = False
        for line in file.read_text(errors="replace").splitlines():
            if "+++ exited with" in line or "+++ killed by" in line:
                terminal = True
                continue
            match = re.match(r"^(\d+\.\d+)\s+(.*)$", line)
            if not match:
                if "fsync(" in line or "fdatasync(" in line:
                    gaps.append(f"undecodable flush in thread {tid}")
                continue
            at = seconds_ns(match[1])
            text = match[2]
            process_text = text
            if re.match(r"(?:clone3?|fork|vfork)\(", text) and "<unfinished ...>" in text:
                pending_process = (text.split("<unfinished ...>", 1)[0], at)
                continue
            resumed_process = re.match(r"<\.\.\. (?:clone3?|fork|vfork) resumed>(.*)", text)
            if resumed_process:
                if pending_process is None:
                    gaps.append(f"unmatched resumed process start in thread {tid}")
                    continue
                process_text = pending_process[0] + resumed_process[1]
                pending_process = None
            fork = re.match(r"(?:clone3?|fork|vfork)\(.*\)\s+=\s+(\d+)(?:\s|$)", process_text)
            if fork:
                relations.append({"parent": tid, "child": int(fork[1]),
                                  "thread": "CLONE_THREAD" in process_text, "at_ns": at})
            flush = re.match(r"(fsync|fdatasync)\(", text)
            resumed = re.match(r"<\.\.\. (fsync|fdatasync) resumed>", text)
            if flush and "<unfinished ...>" in text:
                if pending is not None:
                    gaps.append(f"overlapping flush in thread {tid}")
                pending = (flush[1], at)
                continue
            if not flush and not resumed:
                continue
            if resumed:
                if pending is None or pending[0] != resumed[1]:
                    gaps.append(f"unmatched resumed flush in thread {tid}")
                    continue
                operation, started = pending
                pending = None
            else:
                operation, started = flush[1], at
            completed = re.search(r"=\s+(-?\d+)(?:\s+[^<]*)?\s+<(\d+\.\d+)>\s*$", text)
            if not completed:
                gaps.append(f"missing flush result/duration in thread {tid}")
                continue
            records.append({"tid": tid, "start_ns": started,
                            "end_ns": started + seconds_ns(completed[2]),
                            "operation": operation, "success": int(completed[1]) == 0})
        if pending_process is not None:
            gaps.append(f"unfinished process start in thread {tid}")
        if pending is not None:
            gaps.append(f"unfinished native flush in thread {tid}")
        if not terminal:
            gaps.append(f"missing terminal strace record for thread {tid}")
    if not decoded_files:
        gaps.append("no process-specific strace files")
    return {"events": records, "relations": relations, "gaps": sorted(set(gaps))}


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def timestamp_ns(value: str) -> int:
    """Parse tracerpt's ISO timestamp without discarding its 100ns precision."""
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:?\d\d)", value)
    if not match:
        raise ValueError("unsupported ETW timestamp")
    offset = "+00:00" if match[3] == "Z" else match[3]
    dt = datetime.fromisoformat(match[1] + offset)
    return int(dt.timestamp()) * 1_000_000_000 + int(((match[2] or "") + "000000000")[:9])


def xml_events(path: Path):
    """Keep numeric System fields separate from RenderingInfo display strings.

    Classic kernel events share the SystemTrace provider. Their event-class
    GUID is in ExtendedTracingInfo/EventGuid, not System/Provider. Some
    tracerpt versions cannot render Thread V3's variable tail; its documented
    ProcessId/TThreadId uint32 prefix is still usable (Thread_V2_TypeGroup1).
    """
    for _, element in ET.iterparse(path, events=("end",)):
        if local_name(element.tag) != "Event":
            continue
        system = next((x for x in element if local_name(x.tag) == "System"), None)
        fields = {}
        provider = ""
        opcode = version = None
        at = None
        header_pid = header_tid = None
        if system is not None:
            for node in system:
                tag = local_name(node.tag)
                if tag == "Provider":
                    provider = node.get("Guid", "").strip("{}").lower()
                elif tag == "Opcode":
                    opcode = integer(node.text)
                elif tag == "Version":
                    version = integer(node.text)
                elif tag == "TimeCreated":
                    at = timestamp_ns(node.get("SystemTime", ""))
                elif tag == "Execution":
                    header_pid = integer(node.get("ProcessID"))
                    header_tid = integer(node.get("ThreadID"))
        for node in element.iter():
            tag = local_name(node.tag)
            if tag == "EventGuid" and node.text:
                provider = node.text.strip().strip("{}").lower()
            elif tag == "Data" and node.get("Name"):
                fields[normalize(node.get("Name"))] = node.text or ""
        processing = next((x for x in element if local_name(x.tag) == "ProcessingErrorData"), None)
        error = None
        if processing is not None:
            error = "tracerpt could not decode this event"
            payload = next((x.text for x in processing if local_name(x.tag) == "EventPayload"), "")
            if provider == THREAD and opcode in (1, 2, 3, 4) and version in (2, 3):
                try:
                    raw = bytes.fromhex(payload or "")
                    if len(raw) < 8:
                        raise ValueError("short Thread ProcessId/TThreadId prefix")
                    pid, tid = struct.unpack_from("<II", raw)
                    fields.update(processid=str(pid), tthreadid=str(tid))
                    error = None
                except ValueError:
                    error = "invalid Thread ProcessId/TThreadId prefix"
        yield {"provider": provider, "opcode": opcode, "version": version,
               "at_ns": at, "pid": header_pid, "tid": header_tid,
               "fields": fields, "decode_error": error}
        element.clear()


def trace_etw(path: Path, summary_path: Path) -> dict:
    records, relations, gaps, thread_rows, flush_rows = [], [], [], {}, []
    pending = {}
    if not path.is_file():
        return {"events": [], "relations": [], "gaps": ["no decoded ETW XML"]}
    losses: dict[str, list[int]] = {"events": [], "buffers": []}
    if summary_path.is_file():
        raw = summary_path.read_bytes()
        summary = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
        for kind, value in re.findall(r"(Events\s*Lost|Lost\s*Events|Buffers\s*Lost|Lost\s*Buffers)\s*(?:[:=]\s*)?(\d+)", summary, flags=re.I):
            losses["events" if "event" in kind.lower() else "buffers"].append(int(value))
        for kind, value in re.findall(r"<(EventsLost|LostEvents|BuffersLost|LostBuffers)[^>]*>\s*(\d+)\s*</", summary, flags=re.I):
            losses["events" if "event" in kind.lower() else "buffers"].append(int(value))
    try:
        for row in xml_events(path):
            provider, opcode, at, values = row["provider"], row["opcode"], row["at_ns"], row["fields"]
            if opcode == 0 and provider in {"9e814aad-3204-11d2-9a82-006008a86939", "68fdd900-4a3e-11d1-84f4-0000f80464e3"}:
                for field, kind in (("eventslost", "events"), ("bufferslost", "buffers")):
                    value = integer(values.get(field))
                    if value is not None:
                        losses[kind].append(value)
            if row["decode_error"] and ((provider == FILE_IO and opcode in (73, 76)) or (provider in {THREAD, PROCESS} and opcode in (1, 3))):
                gaps.append(row["decode_error"])
            if provider == THREAD and opcode in (1, 3):
                tid = integer(values.get("ttid", values.get("tthreadid", values.get("threadid"))))
                pid = integer(values.get("processid", values.get("pid")))
                if tid is not None and pid is not None:
                    thread_rows.setdefault(tid, []).append((at, pid))
            elif provider == PROCESS and opcode in (1, 3):
                pid = integer(values.get("processid", values.get("pid")))
                parent = integer(values.get("parentid", values.get("parentprocessid")))
                if pid is not None and parent is not None:
                    relations.append({"parent": parent, "child": pid, "thread": False, "at_ns": at})
            elif provider == FILE_IO and opcode in (73, 76):
                if at is None:
                    gaps.append("native file event lacks timestamp")
                    continue
                irp = values.get("irpptr", values.get("irp"))
                if not irp:
                    gaps.append("native file event lacks operation identity")
                    continue
                if opcode == 73:
                    if irp in pending:
                        gaps.append("overlapping FileIo flush identity")
                    pending[irp] = row
                elif irp in pending:
                    first = pending.pop(irp)
                    tid = integer(first["fields"].get("ttid")) or first["tid"]
                    status = integer(values.get("ntstatus", values.get("status")))
                    if status is None:
                        gaps.append("native flush lacks completion status")
                    if at < first["at_ns"]:
                        gaps.append("native flush completion precedes its start")
                    flush_rows.append({"tid": tid, "header_tid": first["tid"], "pid": first["pid"], "start_ns": first["at_ns"],
                                       "end_ns": at, "operation": "FileIoFlush", "success": status == 0})
    except (ET.ParseError, ValueError) as error:
        gaps.append("ETW decode incomplete: " + str(error))
    if pending:
        gaps.append(f"{len(pending)} native flushes lack completion events")
    if any(not values for values in losses.values()):
        gaps.append("ETW loss accounting unavailable")
    if any(value for values in losses.values() for value in values):
        gaps.append("ETW reports lost events or buffers")
    for row in flush_rows:
        candidates = sorted(thread_rows.get(row["tid"], []), key=lambda item: -1 if item[0] is None else item[0])
        before = [pair for pair in candidates if pair[0] is not None and pair[0] <= row["start_ns"]]
        if before:
            row["pid"] = before[-1][1]
        elif len({pid for _, pid in candidates}) == 1:
            row["pid"] = candidates[0][1]
        elif row["header_tid"] != row["tid"]:
            row["pid"] = None
        if row["pid"] in (None, 4294967295):
            gaps.append("native flush has unresolved process identity")
        records.append(row)
    return {"events": records, "relations": relations, "gaps": sorted(set(gaps)),
            "loss_values": losses}


def count_interval(trace: dict, interval: dict) -> dict:
    root = interval["pid"]
    begin, end = interval["epoch_start_ns"], interval["epoch_reply_ns"]
    owner = {root: root}
    descendants = {root}
    relations = trace["relations"]
    for _ in range(len(relations) + 1):
        changed = False
        for row in relations:
            parent, child = row["parent"], row["child"]
            if parent in owner and child not in owner:
                owner[child] = owner[parent] if row["thread"] else child
                descendants.add(owner[child])
                changed = True
        if not changed:
            break
    events = []
    for row in trace["events"]:
        pid = row.get("pid", owner.get(row["tid"], row["tid"]))
        if pid in descendants and begin <= row["start_ns"] and row["end_ns"] <= end:
            events.append({**row, "pid": pid})
    successful = [row for row in events if row["success"]]
    return {"native_flushes": len(successful) if not trace["gaps"] else None,
            "observed_successful_flushes": len(successful),
            "observed_failed_flushes": len(events) - len(successful),
            "by_operation": dict(Counter(row["operation"] for row in successful)),
            "by_process": dict(Counter(str(row["pid"]) for row in successful)),
            "coverage_gaps": list(trace["gaps"])}
