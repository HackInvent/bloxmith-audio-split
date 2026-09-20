#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Role: Verifies audio split block behavior for the audio split block.
# File Name: F5.18_audio_split_block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-05-10
# -----------------------------------------------------------------------------

"""F5.18 - Bloc Audio split."""

# Test cases:
# - FB1/FB2/FB4/FB5 - Run text -> audio_split -> display in centralized and active runtime, verify chunk files and List/Iterator-compatible JSON output.
# - FB1/FB3 - Execute directly from config.audio_path and custom work_dir.
# - FB4 - Render/update the block-owned inspector and verify numeric config normalization.
# - FB4 - Ignore removed output_dir config and rely only on canonical work_dir.
# - FB5 - Verify describe_block exposes the discovered business block.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
import json
import math
import shutil
import sys
import wave


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from blocs.audio_split.block import AudioSplitBlock
from bloxsmith_app.block_runtime import BlockRuntimeContext
from bloxsmith_app.block_ui import handle_block_ui_action, render_block_inspector_panel, render_block_modal
from bloxsmith_app.graph_introspection import describe_block

from ui_smoke_common import (
    create_run_api,
    data_edge,
    display_node,
    expect,
    graph_payload,
    isolated_server,
    text_node,
    wait_for_run_terminal,
)


def audio_split_node(audio_path: str = "", *, chunk_duration_sec: int = 2, max_chunk_size_mb: int = 24) -> dict[str, Any]:
    return {
        "id": "audio-split-1",
        "kind": "audio_split",
        "title": "Audio split",
        "position": {"x": 360, "y": 120},
        "inputs": [
            {
                "id": 1,
                "name": "audio",
                "title": "Audio",
                "accepts": ["file/path", "audio/*", "message/*"],
                "multiplicity": "many",
            }
        ],
        "outputs": [
            {"id": 1, "name": "liste", "title": "Chunks", "emits": ["application/json", "message/*"], "multiplicity": "many"}
        ],
        "config": {
            "audio_path": audio_path,
            "chunk_duration_sec": chunk_duration_sec,
            "max_chunk_size_mb": max_chunk_size_mb,
            "work_dir": "",
            "experimental_audio_filter_enabled": False,
            "experimental_audio_filter": "highpass=f=90,lowpass=f=7600,afftdn=nr=8,loudnorm=I=-10:TP=-2:LRA=8",
        },
    }


def write_silent_wav(path: Path, *, seconds: int, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = seconds * sample_rate
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frame_count)


def write_sine_wav(path: Path, *, seconds: int, frequency: float = 440.0, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = seconds * sample_rate
    frames = bytearray()
    for index in range(frame_count):
        sample = int(16000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(sample.to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(bytes(frames))


def wav_rms(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        raw = wav.readframes(wav.getnframes())
    if not raw:
        return 0.0
    samples = [
        int.from_bytes(raw[index : index + 2], byteorder="little", signed=True)
        for index in range(0, len(raw) - 1, 2)
    ]
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def direct_context(*, root_dir: Path, run_dir: Path, config: dict[str, Any], input_message: str = "") -> BlockRuntimeContext:
    return BlockRuntimeContext(
        run_id="direct-run",
        node_id="audio-split-direct",
        kind="audio_split",
        title="Audio split",
        config=config,
        inputs={},
        input_content_types={},
        input_message=input_message,
        input_ports=(),
        output_ports=(SimpleNamespace(id=1, name="liste"),),
        root_dir=root_dir,
        run_dir=run_dir,
    )


def assert_audio_split_payload(payload: str, *, expected_count: int) -> list[dict[str, Any]]:
    parsed = json.loads(payload)
    expect(isinstance(parsed, list), "Audio split must emit a JSON list.")
    expect(len(parsed) == expected_count, f"Audio split doit emettre {expected_count} chunk(s).")
    previous_start = -1.0
    for index, wrapper in enumerate(parsed, start=1):
        expect(isinstance(wrapper, dict) and "item" in wrapper, "Chaque entree doit etre compatible List: {item: ...}.")
        item = wrapper["item"]
        expect(item.get("chunk_index") == index, "Les chunks doivent etre indexes dans l'ordre.")
        expect(str(item.get("path") or ""), "Every item must contain path.")
        expect(Path(item["path"]).is_file(), "Le fichier chunk doit exister.")
        expect(float(item.get("start_sec") or 0.0) > previous_start, "Les chunks doivent progresser dans le temps.")
        expect(int(item.get("size_bytes") or 0) > 0, "Chaque item doit exposer size_bytes.")
        previous_start = float(item.get("start_sec") or 0.0)
    return parsed


def node_id_by_title(run: dict[str, Any], title: str) -> str:
    """Return the runtime graph node id matching a stable title."""

    for node in run.get("document", {}).get("nodes", []):
        if node.get("title") == title:
            return str(node.get("id") or "")
    raise AssertionError(f"Node title not found in run document: {title}")


def run_audio_split_case(runtime_mode: str) -> None:
    with isolated_server() as server:
        audio_path = server.root_dir / "tmp" / f"audio-split-{runtime_mode}.wav"
        write_silent_wav(audio_path, seconds=5)
        document = graph_payload(
            f"F5 Audio split {runtime_mode}",
            [
                text_node("text-1", "Audio path", str(audio_path), 80, 120),
                audio_split_node(chunk_duration_sec=2),
                display_node("display-1", "Display", 700, 120),
            ],
            [
                data_edge("edge-text-audio-split", "text-1", 1, "audio-split-1", 1),
                data_edge("edge-audio-split-display", "audio-split-1", 1, "display-1", 1),
            ],
        )
        created = create_run_api(server, document, runtime_mode=runtime_mode)
        run = wait_for_run_terminal(server, str(created.get("run_id") or ""), timeout_sec=25)

        logs = "\n".join(run.get("logs", []))
        audio_split_id = node_id_by_title(run, "Audio split")
        node_logs = "\n".join(run.get("node_logs", {}).get(audio_split_id, []))
        expect(run.get("status") == "success", f"The Audio split {runtime_mode} run must succeed.")
        payload = run.get("output_values", {}).get(f"{audio_split_id}:1", {}).get("value") or ""
        parsed = assert_audio_split_payload(payload, expected_count=3)
        log_text = f"{logs}\n{node_logs}"
        expect("chunk 1" in log_text and "[done] Audio split" in log_text, "Audio split doit tracer chaque chunk.")
        expect(parsed[0]["item"]["path"].endswith(".wav"), "Audio split doit conserver l'extension source.")
        if runtime_mode == "zeromq_active":
            expect(
                run.get("results", {}).get(audio_split_id, {}).get("transport") == "zeromq_active",
                "audio_split must run through zeromq_active.",
            )


def test_direct_runtime() -> None:
    with isolated_server() as server:
        audio_path = server.root_dir / "tmp" / "direct.wav"
        write_sine_wav(audio_path, seconds=4)
        block = AudioSplitBlock()
        filter_value = "highpass=f=1000,lowpass=f=1800,volume=8dB"
        result = block.execute_runtime(
            direct_context(
                root_dir=server.root_dir,
                run_dir=server.root_dir / "runs" / "direct",
                config={
                    "audio_path": str(audio_path),
                    "chunk_duration_sec": 2,
                    "max_chunk_size_mb": 1,
                    "work_dir": "tmp/audio-split-output",
                    "experimental_audio_filter_enabled": True,
                    "experimental_audio_filter": filter_value,
                },
            )
        )
        expect(result.status == "success", "Audio split direct doit reussir.")
        parsed = assert_audio_split_payload(result.outputs[0].value, expected_count=2)
        expect(result.metadata.get("audio_split", {}).get("chunk_count") == 2, "The metadata must expose chunk_count.")
        expect(
            result.metadata.get("audio_split", {}).get("experimental_audio_filter_enabled") is True,
            "The metadata must expose whether the experimental filter is on.",
        )
        expect(
            result.metadata.get("audio_split", {}).get("experimental_audio_filter") == filter_value,
            "The metadata must expose the applied experimental filter.",
        )
        expect("filtre audio experimental" in "\n".join(result.logs), "Les logs doivent tracer le filtre experimental.")
        expect("ffmpeg " in "\n".join(result.logs) and "-af" in "\n".join(result.logs), "Les logs doivent tracer la commande ffmpeg filtree.")
        filtered_chunk = Path(parsed[0]["item"]["path"])
        expect(
            wav_rms(filtered_chunk) < wav_rms(audio_path) * 0.50,
            "Le filtre highpass/lowpass brutal doit modifier le signal audio mesure.",
        )


def test_inspector_contract() -> None:
    node = audio_split_node("fixtures/audio.wav", chunk_duration_sec=90, max_chunk_size_mb=10)
    rendered = render_block_inspector_panel("audio_split", {"node": node})
    html = str(rendered.get("html") or "")
    expect("data-audio-split-inspector-root" in html, "Le panneau inspecteur Audio split doit venir du bloc.")
    expect("data-path-browser" in html, "Le panneau Audio split doit utiliser le path browser commun.")
    expect('data-path-browser-select-mode="file"' in html, "Le chemin audio doit utiliser la selection fichier.")
    expect('data-path-browser-select-mode="directory"' in html, "Le repertoire de travail doit utiliser la selection dossier.")
    expect("data-audio-split-chunk-duration-sec" in html, "Le panneau doit exposer la duree des chunks.")
    expect("data-audio-split-max-chunk-size-mb" in html, "Le panneau doit exposer la taille max.")
    expect("data-audio-split-work-dir" in html, "Le panneau doit exposer le repertoire temporaire des splits.")
    expect("data-audio-split-experimental-audio-filter" in html, "Le panneau doit exposer le filtre experimental.")
    expect("data-block-apply" in html, "Le panneau Audio split doit exposer le bouton Appliquer.")
    expect(rendered.get("context", {}).get("inspector_title") == "Audio split", "The inspector title must come from the block.")

    ports_rendered = render_block_inspector_panel("audio_split", {"node": node, "inspector_tab": "ports"})
    ports_html = str(ports_rendered.get("html") or "")
    expect('data-inspector-panel-tab="ports"' in ports_html, "The panel must expose the Ports tab.")
    expect("data-add-input-port" in ports_html, "The panel must keep the add-input action visible in the DOM.")
    expect("data-add-output-port" in ports_html, "The panel must keep the add-output action visible in the DOM.")
    expect("data-add-input-port type=\"button\" disabled" in ports_html, "Audio split must not allow adding an input.")
    expect("data-add-output-port type=\"button\" disabled" in ports_html, "Audio split must not allow adding an output.")

    result = handle_block_ui_action(
        "audio_split",
        {
            "node": node,
            "action": "inspector_update_audio_split",
            "values": {
                "audio_path": "./next.wav",
                "chunk_duration_sec": "0",
                "max_chunk_size_mb": "0",
                "work_dir": "./chunks",
                "experimental_audio_filter_enabled": True,
                "experimental_audio_filter": "highpass=f=120,lowpass=f=5000",
            },
        },
    )
    config = result.get("node_patch", {}).get("config", {})
    expect(config.get("chunk_duration_sec") == 1, "The duration must be clamped to one second minimum.")
    expect(config.get("max_chunk_size_mb") == 1, "The maximum size must be clamped to 1 MB minimum.")
    expect(config.get("audio_path") == "./next.wav", "The audio path must be kept.")
    expect(config.get("work_dir") == "./chunks", "The temporary directory must be kept.")
    expect(config.get("experimental_audio_filter_enabled") is True, "The experimental filter must be switchable.")
    expect(config.get("experimental_audio_filter") == "highpass=f=120,lowpass=f=5000", "Le filtre experimental must be editable.")

    modal = render_block_modal("audio_split", {"node": node, "runtime": {}})
    modal_html = str(modal.get("html") or "")
    expect("data-audio-split-modal-root" in modal_html, "Le modal Audio split doit venir du bloc.")
    expect("data-path-browser" in modal_html, "Le modal Audio split doit utiliser le path browser commun.")
    expect("data-audio-split-apply" in modal_html, "Le modal Audio split doit exposer son action Appliquer.")

    modal_result = handle_block_ui_action(
        "audio_split",
        {
            "node": node,
            "action": "modal_update_audio_split",
            "values": {
                "audio_path": "./modal.wav",
                "chunk_duration_sec": "3",
                "max_chunk_size_mb": "4",
                "work_dir": "./modal-chunks",
                "experimental_audio_filter_enabled": False,
                "experimental_audio_filter": "",
            },
        },
    )
    modal_config = modal_result.get("node_patch", {}).get("config", {})
    expect(modal_config.get("audio_path") == "./modal.wav", "The modal must persist the audio path.")
    expect(modal_config.get("work_dir") == "./modal-chunks", "The modal must persist the temporary directory.")


def test_removed_output_dir_config_is_ignored() -> None:
    block = AudioSplitBlock()
    config = block._runtime_config({"output_dir": "./legacy-chunks"})
    expect(config.get("work_dir") == "", "output_dir retire ne doit plus alimenter work_dir.")


def test_introspection() -> None:
    description = describe_block("audio_split")
    expect(description["title"] == "Audio split", "The Audio split block must be discovered by introspection.")
    expect(description["default_config"]["chunk_duration_sec"] == 60, "The default duration must be introspected.")
    expect(description["default_config"]["max_chunk_size_mb"] == 24, "The default maximum size must be introspected.")
    expect(
        description["default_config"]["experimental_audio_filter_enabled"] is False,
        "The experimental filter must be off by default.",
    )
    expect(description["capabilities"]["runtime_executable"], "audio_split must be runtime_executable.")
    expect(description["capabilities"]["active_worker"], "audio_split must be active_worker.")
    expect(description["capabilities"]["file_browser"], "audio_split must expose file and directory browsing.")


def main() -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("[skip] F5.18_audio_split_block: ffmpeg/ffprobe introuvables")
        return
    run_audio_split_case("centralized")
    run_audio_split_case("zeromq_active")
    test_direct_runtime()
    test_inspector_contract()
    test_removed_output_dir_config_is_ignored()
    test_introspection()
    expect(AudioSplitBlock().kind == "audio_split", "The Audio split block must expose its kind.")
    print("[ok] F5.18_audio_split_block")


if __name__ == "__main__":
    main()
