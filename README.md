# Audio Split

<!-- block-metadata:start -->
[![Block version: unversioned](https://img.shields.io/badge/block-unversioned-lightgrey)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->


## Purpose

Split an audio file into chunk files with `ffmpeg`, then emit a JSON list compatible with List and Iterator.

## Files

- `block.py`: audio path resolution, FFmpeg splitting, size checks, optional filtering and JSON output.
- `model.json`: audio input, list output and chunking defaults.
- `block_modal.html`, `inspector_panel.html`, `assets/`: configuration UI.
- `node_card.html`: block-specific canvas summary.

## Ports

- Optional input **`audio`**, ID 1: a file path from `file/path`, `audio/*` or `message/*`.
- Output **`liste`**, ID 1: a JSON list compatible with Iterator.

Each emitted item has this shape:

```json
{
  "item": {
    "path": "chunks/audio-chunk-0001.wav",
    "file_name": "audio-chunk-0001.wav",
    "chunk_index": 1,
    "start_sec": 0,
    "end_sec": 60,
    "duration_sec": 60,
    "size_bytes": 123456,
    "source_path": "media/audio.wav"
  }
}
```

## Configuration

- `audio_path`: fallback path when `audio` is not connected.
- `chunk_duration_sec`: target duration in seconds.
- `max_chunk_size_mb`: maximum chunk size in MB. Duration is automatically reduced if a chunk exceeds it.
- `work_dir`: optional directory for split files. If empty, the block creates a temporary run directory.
- `experimental_audio_filter_enabled`: enable the experimental FFmpeg filter before writing chunks.
- `experimental_audio_filter`: FFmpeg `-af` chain, for example `highpass=f=90,lowpass=f=7600,afftdn=nr=8,loudnorm=I=-10:TP=-2:LRA=8`.

## Runtime behavior

With filtering disabled, `ffmpeg -c copy` preserves the source codec/container. Enabling filtering requires FFmpeg to re-encode chunks to apply `-af`. The same `execute_runtime()` path serves `centralized` and `zeromq_active`.

## UI

The inspector and modal edit the fallback audio path, chunk sizes, working directory and filter through `inspector_update_audio_split` or `modal_update_audio_split`. Changes remain pending until Apply (`Appliquer`). The shared `CWPathBrowser` selects the audio file in file mode and the working directory in directory mode.

The Ports tab exposes the fixed `audio` input and `liste` output but disables adding ports. The canvas card summarizes the source, duration and size limit; the editor retains generic responsibility for ports, dragging, status and links. The block-owned modal keeps the generic title binding and exposes the same configuration, ports and runtime state.

## Maintenance and tests

Keep FFmpeg commands, size control and item shape inside this block. Changes to item fields must update Iterator/List interoperability tests. UI fixtures use relative paths.

From the private integration workspace:

```sh
python3 -B tests/run_tests.py audio_split
```

## Compatibility policy

[compatibility.json](compatibility.json) records HackInvent's verified BloxSmith versions and test evidence. Only the versions listed above have been verified, using the block-owned suites in a **bundled-block test installation**. This is not a certification of managed-package installation, every browser/OS, or live provider availability. Other framework versions are unverified, not necessarily incompatible.

The block-version badge follows `model.json`, not a published Git tag. `unversioned` means that no block release version is declared; no number is inferred from the framework version. The framework still uses `model.json` for its runtime/install contract; the tester-owned JSON does not replace it. Official integration tests run in the private `bloxmith-blocs` workspace. Test helpers and the proprietary framework are not bundled in this public block repository.
