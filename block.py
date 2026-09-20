# -----------------------------------------------------------------------------
# Role: Implements the audio split block runtime and UI contract.
# File Name: block.py
# Author: Alexandre EL
# Email: alex@hackinvent.com
# Created Date: 2026-05-10
# -----------------------------------------------------------------------------

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
import json
import os
import re
import shlex
import shutil
import subprocess

from bloxsmith_app.block_api import (
    APPLICATION_JSON,
    BlockDefinition,
    BlockRuntimeContext,
    BlockRuntimeOutput,
    BlockRuntimeResult,
    configured_runs_dir,
    render_inspector_template,
    render_node_card_template,
    render_path_browser_control,
)


AUDIO_SPLIT_DEFAULT_CHUNK_DURATION_SEC = 60
AUDIO_SPLIT_DEFAULT_MAX_CHUNK_SIZE_MB = 24
AUDIO_SPLIT_DEFAULT_EXPERIMENTAL_AUDIO_FILTER = "highpass=f=90,lowpass=f=7600,afftdn=nr=8,loudnorm=I=-10:TP=-2:LRA=8"
AUDIO_SPLIT_MIN_CHUNK_DURATION_SEC = 1
AUDIO_SPLIT_MAX_CHUNK_DURATION_SEC = 24 * 60 * 60
AUDIO_SPLIT_MIN_MAX_CHUNK_SIZE_MB = 1
AUDIO_SPLIT_MAX_MAX_CHUNK_SIZE_MB = 1024
AUDIO_SPLIT_PROCESS_TIMEOUT_SEC = 600
AUDIO_SPLIT_SUPPORTED_EXTENSIONS = (
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
    ".webm",
)


class AudioSplitBlockError(ValueError):
    """Raised when Audio split cannot produce chunk files."""


class AudioSplitBlockCancelled(RuntimeError):
    """Raised when the runtime asks the block to stop."""


@dataclass(frozen=True)
class AudioChunk:
    """Structured data used by this block implementation."""
    path: Path
    index: int
    start_sec: float
    duration_sec: float
    size_bytes: int


# Functional behavior:
# FB1 - Resolve an input audio path from the input port or from config.audio_path.
# FB2 - Split the audio with ffmpeg while preserving the source container/codec.
# FB3 - Respect both a target duration and a max file size per chunk by shortening chunks when needed.
# FB4 - Emit a standard List-compatible JSON array of {"item": {...}} objects for Iterator.
# FB5 - Run through the generic runtime path used by both centralized and zeromq_active execution modes.
class AudioSplitBlock(BlockDefinition):
    """Autonomous block implementation for `AudioSplitBlock`."""
    kind = "audio_split"

    def render_node_card(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the Audio Split canvas card body from the block-owned template."""

        config = self._ui_config(node)
        audio_path = str(config.get("audio_path") or "audio input")
        return render_node_card_template(
            block=self,
            node=node,
            node_classes=["audio-split-node"],
            replacements={
                "title": node.get("title") or self.default_title(),
                "audio_path": self._truncate(audio_path, 38),
                "duration": f"{config['chunk_duration_sec']}s",
                "max_size": f"{config['max_chunk_size_mb']} Mo",
            },
        )

    def render_modal(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the Audio Split modal with shared file and directory browsers."""

        config = self._ui_config(node)
        template = (self.directory / "block_modal.html").read_text(encoding="utf-8")
        html = self._render_generic_modal_template(
            template=(
                self._apply_ui_replacements(template, config, id_prefix="audioSplitModal")
                .replace("{{ config_fields_html }}", self._render_modal_technical_config_fields(node))
            ),
            node=node,
            payload=payload or {},
        )
        return {
            "html": html,
            "context": {
                "node_id": str(node.get("id") or ""),
                "node_kind": self.kind,
                **config,
            },
        }

    def render_inspector_panel(self, *, node: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Render the block-owned inspector panel HTML for the selected node.

        Args:
            node: Serialized graph node handled by the block.
            payload: Optional UI or runtime payload provided by the framework.
        """
        config = self._ui_config(node)
        template = (self.directory / "inspector_panel.html").read_text(encoding="utf-8")
        html = render_inspector_template(
            template=self._apply_ui_replacements(template, config, id_prefix="audioSplitInspector"),
            node={**node, "type": self.kind, "kind": self.kind},
            payload=payload,
            show_duplicate=False,
        )
        return {
            "html": html,
            "context": {
                "node_id": str(node.get("id") or ""),
                "full_panel": True,
                "inspector_title": str(node.get("title") or self.default_title()),
            },
        }

    def handle_ui_action(
        self,
        *,
        node: dict[str, Any],
        action: str,
        values: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist inspector edits for chunking and split working-directory settings."""

        if action not in {"inspector_update_audio_split", "modal_update_audio_split"}:
            return super().handle_ui_action(node=node, action=action, values=values, payload=payload)
        enabled = self._bool(values.get("experimental_audio_filter_enabled", False))
        work_dir = str(values.get("work_dir") or "").strip()
        return {
            "node_patch": {
                "config": {
                    "audio_path": str(values.get("audio_path") or "").strip(),
                    "chunk_duration_sec": self._normalize_chunk_duration_sec(values.get("chunk_duration_sec")),
                    "max_chunk_size_mb": self._normalize_max_chunk_size_mb(values.get("max_chunk_size_mb")),
                    "work_dir": work_dir,
                    "experimental_audio_filter_enabled": enabled,
                    "experimental_audio_filter": str(
                        values.get("experimental_audio_filter") or AUDIO_SPLIT_DEFAULT_EXPERIMENTAL_AUDIO_FILTER
                    ).strip(),
                }
            },
            "message": f"[audio-split] Filtre experimental {'active' if enabled else 'desactive'}.",
            "rerender_inspector": False,
        }

    def _apply_ui_replacements(self, template: str, config: dict[str, Any], *, id_prefix: str) -> str:
        """Fill Audio Split UI placeholders shared by inspector and modal templates."""

        replacements = {
            "audio_path_browser_html": self._render_audio_path_browser(config, input_id=f"{id_prefix}AudioPath"),
            "chunk_duration_sec": escape(str(config.get("chunk_duration_sec") or AUDIO_SPLIT_DEFAULT_CHUNK_DURATION_SEC)),
            "max_chunk_size_mb": escape(str(config.get("max_chunk_size_mb") or AUDIO_SPLIT_DEFAULT_MAX_CHUNK_SIZE_MB)),
            "work_dir_browser_html": self._render_work_dir_browser(config, input_id=f"{id_prefix}WorkDir"),
            "experimental_audio_filter_enabled_checked": (
                "checked" if config.get("experimental_audio_filter_enabled") else ""
            ),
            "experimental_audio_filter": escape(
                str(config.get("experimental_audio_filter") or AUDIO_SPLIT_DEFAULT_EXPERIMENTAL_AUDIO_FILTER)
            ),
        }
        html = template
        for key, value in replacements.items():
            html = html.replace(f"{{{{ {key} }}}}", str(value))
        return html

    def _render_audio_path_browser(self, config: dict[str, Any], *, input_id: str) -> str:
        """Render the shared file browser used to select the default audio file."""

        return render_path_browser_control(
            input_id=input_id,
            label="Chemin audio par defaut",
            value=str(config.get("audio_path") or ""),
            placeholder="./audio.mp3",
            input_attrs="data-audio-split-audio-path",
            select_mode="file",
        )

    def _render_work_dir_browser(self, config: dict[str, Any], *, input_id: str) -> str:
        """Render the shared directory browser used to choose the split work directory."""

        return render_path_browser_control(
            input_id=input_id,
            label="Temporary split directory",
            value=str(config.get("work_dir") or ""),
            placeholder="Leave empty for the run temporary folder",
            input_attrs="data-audio-split-work-dir",
            select_mode="directory",
        )

    def _render_modal_technical_config_fields(self, node: dict[str, Any]) -> str:
        """Render Audio Split config keys not owned by the dedicated modal controls."""

        config = self.default_config()
        node_config = node.get("config")
        if isinstance(node_config, dict):
            config.update(node_config)
        hidden_keys = {
            "audio_path",
            "chunk_duration_sec",
            "max_chunk_size_mb",
            "work_dir",
            "experimental_audio_filter_enabled",
            "experimental_audio_filter",
        }
        fields = [
            self._render_generic_modal_config_field(key, value)
            for key, value in config.items()
            if str(key) not in hidden_keys
        ]
        return "\n".join(fields) if fields else '<div class="ports-editor-empty">No technical attribute.</div>'

    def preview_received(self, *, node: Any, **runtime_services: Any) -> str:
        """Return a compact preview value for runtime display surfaces.

        Args:
            node: Serialized graph node handled by the block.
            runtime_services: Runtime services value used by this block helper.
        """
        config = getattr(node, "config", {}) if isinstance(getattr(node, "config", {}), dict) else {}
        return str(config.get("audio_path") or "audio non defini")

    def execute_runtime(self, context: BlockRuntimeContext) -> BlockRuntimeResult:
        """Execute the block through the generic runtime context and return runtime outputs.

        Args:
            context: Generic runtime context injected by the execution engine.
        """
        logs: list[str] = []
        try:
            config = self._runtime_config(context.config)
            audio_path = self._resolve_audio_path(context=context, config=config)
            self._require_tools()
            duration_sec = self._probe_duration_sec(audio_path)
            output_dir = self._resolve_output_dir(context=context, config=config)
            display_path = self._display_path(audio_path, context.root_dir)
            self._emit_log(
                context,
                logs,
                f"[audio-split] {context.node_id}: fichier={display_path} "
                f"duree={duration_sec:.2f}s taille={self._format_bytes(audio_path.stat().st_size)}.",
            )
            self._emit_log(
                context,
                logs,
                f"[audio-split] {context.node_id}: fenetre={config['chunk_duration_sec']}s "
                f"max={config['max_chunk_size_mb']} Mo travail={self._display_path(output_dir, context.root_dir)}.",
            )
            if config["experimental_audio_filter_enabled"]:
                self._emit_log(
                    context,
                    logs,
                    f"[audio-split] {context.node_id}: filtre audio experimental={config['experimental_audio_filter']}.",
                )
            chunks = self._split_audio(
                audio_path=audio_path,
                output_dir=output_dir,
                duration_sec=duration_sec,
                chunk_duration_sec=float(config["chunk_duration_sec"]),
                max_chunk_size_bytes=int(config["max_chunk_size_mb"]) * 1024 * 1024,
                audio_filter=str(config["experimental_audio_filter"] if config["experimental_audio_filter_enabled"] else ""),
                context=context,
                logs=logs,
            )
            payload = self._serialize_chunks(chunks=chunks, source_path=audio_path, root_dir=context.root_dir)
            outputs = [
                BlockRuntimeOutput(
                    port_id=int(getattr(port, "id", 0) or 0),
                    port_name=str(getattr(port, "name", "") or ""),
                    value=payload,
                    content_type=APPLICATION_JSON,
                )
                for port in context.output_ports
            ]
            self._emit_log(context, logs, f"[done] Audio split {context.node_id}: {len(chunks)} chunk(s).")
            return BlockRuntimeResult(
                status="success",
                outputs=outputs,
                logs=logs,
                last_message=payload,
                content_type=APPLICATION_JSON,
                worker_received=display_path,
                metadata={
                    "audio_split": {
                        "source_path": display_path,
                        "work_dir": self._display_path(output_dir, context.root_dir),
                        "output_dir": self._display_path(output_dir, context.root_dir),
                        "chunk_count": len(chunks),
                        "chunk_duration_sec": config["chunk_duration_sec"],
                        "max_chunk_size_mb": config["max_chunk_size_mb"],
                        "experimental_audio_filter_enabled": config["experimental_audio_filter_enabled"],
                        "experimental_audio_filter": config["experimental_audio_filter"]
                        if config["experimental_audio_filter_enabled"]
                        else "",
                    }
                },
            )
        except AudioSplitBlockCancelled as exc:
            return BlockRuntimeResult(status="cancelled", logs=logs, error=str(exc), exit_code=130)
        except AudioSplitBlockError as exc:
            return BlockRuntimeResult(status="failed", logs=logs, error=str(exc), exit_code=1)

    def _runtime_config(self, raw_config: dict[str, Any] | None) -> dict[str, Any]:
        """Normalize persisted Audio Split config using canonical keys only."""

        config = raw_config if isinstance(raw_config, dict) else {}
        return {
            "audio_path": str(config.get("audio_path") or "").strip(),
            "chunk_duration_sec": self._normalize_chunk_duration_sec(config.get("chunk_duration_sec")),
            "max_chunk_size_mb": self._normalize_max_chunk_size_mb(config.get("max_chunk_size_mb")),
            "work_dir": str(config.get("work_dir") or "").strip(),
            "experimental_audio_filter_enabled": self._bool(config.get("experimental_audio_filter_enabled", False)),
            "experimental_audio_filter": str(
                config.get("experimental_audio_filter") or AUDIO_SPLIT_DEFAULT_EXPERIMENTAL_AUDIO_FILTER
            ).strip(),
        }

    def _ui_config(self, node: dict[str, Any]) -> dict[str, Any]:
        """Provide internal AudioSplitBlock behavior for `_ui_config`.

        Args:
            node: Serialized graph node handled by the block.
        """
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        return self._runtime_config(config)

    def _truncate(self, value: str, max_length: int) -> str:
        """Return a compact one-line label for the node card preview."""

        text = str(value or "").replace("\n", " ").strip()
        return text if len(text) <= max_length else f"{text[: max_length - 1]}..."

    def _normalize_chunk_duration_sec(self, value: Any) -> int:
        """Normalize a raw value into the format expected by the block.

        Args:
            value: Value to normalize, render, serialize, or process.
        """
        return self._clamp_int(
            value,
            default=AUDIO_SPLIT_DEFAULT_CHUNK_DURATION_SEC,
            minimum=AUDIO_SPLIT_MIN_CHUNK_DURATION_SEC,
            maximum=AUDIO_SPLIT_MAX_CHUNK_DURATION_SEC,
        )

    def _normalize_max_chunk_size_mb(self, value: Any) -> int:
        """Normalize a raw value into the format expected by the block.

        Args:
            value: Value to normalize, render, serialize, or process.
        """
        return self._clamp_int(
            value,
            default=AUDIO_SPLIT_DEFAULT_MAX_CHUNK_SIZE_MB,
            minimum=AUDIO_SPLIT_MIN_MAX_CHUNK_SIZE_MB,
            maximum=AUDIO_SPLIT_MAX_MAX_CHUNK_SIZE_MB,
        )

    def _clamp_int(self, value: Any, *, default: int, minimum: int, maximum: int) -> int:
        """Provide internal AudioSplitBlock behavior for `_clamp_int`.

        Args:
            value: Value to normalize, render, serialize, or process.
            default: Default value used when normalization fails.
            minimum: Lower bound accepted by the normalizer.
            maximum: Upper bound accepted by the normalizer.
        """
        try:
            parsed = int(float(value))
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _bool(self, value: Any) -> bool:
        """Normalize a raw boolean-like configuration value.

        Args:
            value: Value to normalize, render, serialize, or process.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}

    def _resolve_audio_path(self, *, context: BlockRuntimeContext, config: dict[str, Any]) -> Path:
        """Resolve a configured value against runtime or project context.

        Args:
            context: Generic runtime context injected by the execution engine.
            config: Raw or normalized block configuration.
        """
        raw_value = (
            context.input_value("audio")
            or context.input_value("1")
            or context.input_message
            or config.get("audio_path")
            or ""
        )
        path_text = self._extract_path_value(raw_value)
        if not path_text:
            raise AudioSplitBlockError("Audio split: aucun chemin audio fourni.")
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = context.root_dir / path
        path = path.resolve()
        if not path.is_file():
            raise AudioSplitBlockError(f"Audio split: fichier introuvable: {path_text}")
        if path.suffix.lower() not in AUDIO_SPLIT_SUPPORTED_EXTENSIONS:
            supported = ", ".join(AUDIO_SPLIT_SUPPORTED_EXTENSIONS)
            raise AudioSplitBlockError(f"Audio split: format audio non supporte ({path.suffix or 'sans extension'}). Formats: {supported}")
        return path

    def _extract_path_value(self, raw_value: Any) -> str:
        """Provide internal AudioSplitBlock behavior for `_extract_path_value`.

        Args:
            raw_value: Raw value received from configuration or runtime input.
        """
        if raw_value is None:
            return ""
        if isinstance(raw_value, dict):
            return self._path_from_dict(raw_value)
        text = str(raw_value or "").strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict):
            return self._path_from_dict(parsed) or text
        return text

    def _path_from_dict(self, value: dict[str, Any]) -> str:
        """Provide internal AudioSplitBlock behavior for `_path_from_dict`.

        Args:
            value: Value to normalize, render, serialize, or process.
        """
        for key in ("path", "file_path", "audio_path", "file"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        item = value.get("item")
        if isinstance(item, dict):
            return self._path_from_dict(item)
        if isinstance(item, str) and item.strip():
            return item.strip()
        return ""

    def _resolve_output_dir(self, *, context: BlockRuntimeContext, config: dict[str, Any]) -> Path:
        """Resolve the working directory where chunk files are written for this run."""

        raw_work_dir = str(config.get("work_dir") or "").strip()
        if raw_work_dir:
            output_dir = Path(raw_work_dir).expanduser()
            if not output_dir.is_absolute():
                output_dir = context.root_dir / output_dir
        elif context.run_dir is not None:
            output_dir = context.run_dir / "audio_split" / context.node_id
        else:
            output_dir = configured_runs_dir(context.root_dir) / "audio_split" / context.run_id / context.node_id
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir.resolve()

    def _require_tools(self) -> None:
        """Provide internal AudioSplitBlock behavior for `_require_tools`."""
        missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
        if missing:
            raise AudioSplitBlockError(f"Audio split: outil requis introuvable: {', '.join(missing)}")

    def _probe_duration_sec(self, audio_path: Path) -> float:
        """Provide internal AudioSplitBlock behavior for `_probe_duration_sec`.

        Args:
            audio_path: Filesystem path handled by the block.
        """
        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=AUDIO_SPLIT_PROCESS_TIMEOUT_SEC,
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            raise AudioSplitBlockError(f"Audio split: ffprobe a echoue: {error or result.returncode}")
        try:
            duration_sec = float((result.stdout or "").strip())
        except ValueError as exc:
            raise AudioSplitBlockError("Audio split: audio duration unreadable by ffprobe.") from exc
        if duration_sec <= 0:
            raise AudioSplitBlockError("Audio split: empty or invalid audio duration.")
        return duration_sec

    def _split_audio(
        self,
        *,
        audio_path: Path,
        output_dir: Path,
        duration_sec: float,
        chunk_duration_sec: float,
        max_chunk_size_bytes: int,
        audio_filter: str,
        context: BlockRuntimeContext,
        logs: list[str],
    ) -> list[AudioChunk]:
        """Provide internal AudioSplitBlock behavior for `_split_audio`.

        Args:
            audio_path: Filesystem path handled by the block.
            output_dir: Directory path used by the block runtime.
            duration_sec: Duration in seconds.
            chunk_duration_sec: Duration in seconds.
            max_chunk_size_bytes: Max chunk size bytes value used by this block helper.
            audio_filter: Audio filter value used by this block helper.
            context: Generic runtime context injected by the execution engine.
            logs: Logs value used by this block helper.
        """
        chunks: list[AudioChunk] = []
        cursor = 0.0
        index = 1
        safe_stem = self._safe_file_stem(audio_path.stem)
        suffix = audio_path.suffix.lower() or ".audio"
        while cursor < duration_sec - 0.001:
            self._check_cancelled(context)
            remaining = max(0.0, duration_sec - cursor)
            target_duration = min(chunk_duration_sec, remaining)
            chunk = self._write_bounded_chunk(
                audio_path=audio_path,
                output_dir=output_dir,
                safe_stem=safe_stem,
                suffix=suffix,
                index=index,
                start_sec=cursor,
                target_duration_sec=target_duration,
                max_chunk_size_bytes=max_chunk_size_bytes,
                audio_filter=audio_filter,
                logs=logs,
                context=context,
            )
            chunks.append(chunk)
            mode = "filter" if audio_filter else "copy"
            self._emit_log(
                context,
                logs,
                f"[audio-split] {context.node_id}: chunk {index} "
                f"start={self._format_mmss(chunk.start_sec)} duree={chunk.duration_sec:.2f}s "
                f"taille={self._format_bytes(chunk.size_bytes)} mode={mode} -> {chunk.path.name}",
            )
            cursor += max(0.001, chunk.duration_sec)
            index += 1
        if not chunks:
            raise AudioSplitBlockError("Audio split: aucun chunk produit.")
        return chunks

    def _write_bounded_chunk(
        self,
        *,
        audio_path: Path,
        output_dir: Path,
        safe_stem: str,
        suffix: str,
        index: int,
        start_sec: float,
        target_duration_sec: float,
        max_chunk_size_bytes: int,
        audio_filter: str,
        logs: list[str],
        context: BlockRuntimeContext,
    ) -> AudioChunk:
        """Provide internal AudioSplitBlock behavior for `_write_bounded_chunk`.

        Args:
            audio_path: Filesystem path handled by the block.
            output_dir: Directory path used by the block runtime.
            safe_stem: Safe stem value used by this block helper.
            suffix: Suffix value used by this block helper.
            index: Index value used by this block helper.
            start_sec: Duration in seconds.
            target_duration_sec: Duration in seconds.
            max_chunk_size_bytes: Max chunk size bytes value used by this block helper.
            audio_filter: Audio filter value used by this block helper.
            logs: Logs value used by this block helper.
            context: Generic runtime context injected by the execution engine.
        """
        duration = max(0.001, target_duration_sec)
        output_path = output_dir / f"{safe_stem}-chunk-{index:04d}{suffix}"
        attempts = 0
        while True:
            self._check_cancelled(context)
            attempts += 1
            self._write_chunk_copy(
                audio_path=audio_path,
                output_path=output_path,
                start_sec=start_sec,
                duration_sec=duration,
                audio_filter=audio_filter,
                logs=logs,
                context=context,
            )
            size_bytes = output_path.stat().st_size
            if size_bytes <= max_chunk_size_bytes:
                return AudioChunk(output_path, index, start_sec, duration, size_bytes)
            if duration <= 1.0:
                raise AudioSplitBlockError(
                    "Audio split: a one-second chunk still exceeds the maximum size "
                    f"({self._format_bytes(size_bytes)} > {self._format_bytes(max_chunk_size_bytes)})."
                )
            if attempts >= 16:
                raise AudioSplitBlockError("Audio split: the maximum size could not be met after several attempts.")
            ratio = max_chunk_size_bytes / max(1, size_bytes)
            duration = max(1.0, duration * max(0.10, min(0.90, ratio * 0.92)))

    def _write_chunk_copy(
        self,
        *,
        audio_path: Path,
        output_path: Path,
        start_sec: float,
        duration_sec: float,
        audio_filter: str,
        logs: list[str],
        context: BlockRuntimeContext,
    ) -> None:
        """Provide internal AudioSplitBlock behavior for `_write_chunk_copy`.

        Args:
            audio_path: Filesystem path handled by the block.
            output_path: Filesystem path handled by the block.
            start_sec: Duration in seconds.
            duration_sec: Duration in seconds.
            audio_filter: Audio filter value used by this block helper.
            logs: Logs value used by this block helper.
            context: Generic runtime context injected by the execution engine.
        """
        audio_filter = str(audio_filter or "").strip()
        output_options = (
            ["-af", audio_filter, *self._filtered_audio_codec_options(output_path.suffix)]
            if audio_filter
            else ["-c", "copy"]
        )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-t",
            f"{duration_sec:.3f}",
            "-i",
            str(audio_path),
            "-map",
            "0:a:0",
            "-vn",
            *output_options,
            str(output_path),
        ]
        if audio_filter:
            self._emit_log(context, logs, f"[audio-split] {context.node_id}: ffmpeg {' '.join(shlex.quote(part) for part in command)}")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=AUDIO_SPLIT_PROCESS_TIMEOUT_SEC,
            check=False,
        )
        if result.returncode != 0:
            error = (result.stderr or result.stdout or "").strip()
            raise AudioSplitBlockError(f"Audio split: ffmpeg a echoue: {error or result.returncode}")

    def _filtered_audio_codec_options(self, suffix: str) -> list[str]:
        """Provide internal AudioSplitBlock behavior for `_filtered_audio_codec_options`.

        Args:
            suffix: Suffix value used by this block helper.
        """
        normalized = str(suffix or "").lower()
        if normalized == ".wav":
            return ["-c:a", "pcm_s16le"]
        if normalized in {".mp3", ".mpga", ".mpeg"}:
            return ["-c:a", "libmp3lame", "-q:a", "2"]
        if normalized in {".m4a", ".mp4", ".aac"}:
            return ["-c:a", "aac", "-b:a", "192k"]
        if normalized == ".flac":
            return ["-c:a", "flac"]
        if normalized == ".ogg":
            return ["-c:a", "libvorbis", "-q:a", "5"]
        if normalized == ".webm":
            return ["-c:a", "libopus", "-b:a", "128k"]
        return ["-c:a", "pcm_s16le"]

    def _serialize_chunks(self, *, chunks: list[AudioChunk], source_path: Path, root_dir: Path) -> str:
        """Provide internal AudioSplitBlock behavior for `_serialize_chunks`.

        Args:
            chunks: Chunks value used by this block helper.
            source_path: Filesystem path handled by the block.
            root_dir: Directory path used by the block runtime.
        """
        items = []
        for chunk in chunks:
            end_sec = chunk.start_sec + chunk.duration_sec
            item: dict[str, Any] = {
                "path": str(chunk.path),
                "file_name": chunk.path.name,
                "chunk_index": chunk.index,
                "start_sec": round(chunk.start_sec, 3),
                "end_sec": round(end_sec, 3),
                "duration_sec": round(chunk.duration_sec, 3),
                "size_bytes": chunk.size_bytes,
                "source_path": str(source_path),
            }
            relative_path = self._relative_path(chunk.path, root_dir)
            if relative_path:
                item["relative_path"] = relative_path
            items.append({"item": item})
        return json.dumps(items, ensure_ascii=False, indent=2)

    def _relative_path(self, path: Path, root_dir: Path) -> str:
        """Provide internal AudioSplitBlock behavior for `_relative_path`.

        Args:
            path: Filesystem path handled by the block.
            root_dir: Directory path used by the block runtime.
        """
        try:
            return os.path.relpath(path, root_dir)
        except ValueError:
            return ""

    def _check_cancelled(self, context: BlockRuntimeContext) -> None:
        """Check runtime state and raise or return when execution should stop.

        Args:
            context: Generic runtime context injected by the execution engine.
        """
        checker = context.services.get("cancel_requested") if isinstance(context.services, dict) else None
        if callable(checker) and checker():
            raise AudioSplitBlockCancelled("Audio split: annulation demandee.")

    def _emit_log(self, context: BlockRuntimeContext, logs: list[str], message: str) -> None:
        """Emit a runtime log or event through the injected context.

        Args:
            context: Generic runtime context injected by the execution engine.
            logs: Logs value used by this block helper.
            message: Log or status message to emit.
        """
        append_log = context.services.get("append_log") if isinstance(context.services, dict) else None
        if callable(append_log):
            append_log(message)
        else:
            logs.append(message)

    def _safe_file_stem(self, value: str) -> str:
        """Provide internal AudioSplitBlock behavior for `_safe_file_stem`.

        Args:
            value: Value to normalize, render, serialize, or process.
        """
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")
        return (safe or "audio")[:80]

    def _display_path(self, path: Path, root_dir: Path) -> str:
        """Provide internal AudioSplitBlock behavior for `_display_path`.

        Args:
            path: Filesystem path handled by the block.
            root_dir: Directory path used by the block runtime.
        """
        try:
            return os.path.relpath(path, root_dir)
        except ValueError:
            return str(path)

    def _format_bytes(self, value: int) -> str:
        """Format a value for logs, UI display, or runtime output.

        Args:
            value: Value to normalize, render, serialize, or process.
        """
        if value < 1024:
            return f"{value} B"
        if value < 1024 * 1024:
            return f"{value / 1024:.1f} KiB"
        return f"{value / (1024 * 1024):.2f} MiB"

    def _format_mmss(self, value: float) -> str:
        """Format a value for logs, UI display, or runtime output.

        Args:
            value: Value to normalize, render, serialize, or process.
        """
        total = max(0, int(value))
        minutes, seconds = divmod(total, 60)
        return f"{minutes:02d}:{seconds:02d}"
