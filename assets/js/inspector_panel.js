/**
 * Role: Mounts the audio split block inspector panel frontend.
 * File Name: inspector_panel.js
 * Author: Alexandre EL
 * Email: alex@hackinvent.com
 * Created Date: 2026-05-10
 */

(function () {
  "use strict";

  const registry = (window.CWBlockUiBlocks = window.CWBlockUiBlocks || {});

  /**
   * Bind Audio Split configuration fields for inspector and modal surfaces.
   *
   * @param {HTMLElement} root - Mounted inspector or modal root.
   * @param {object} api - Generic block UI API exposing block actions.
   * @param {object} options - Action name used by the current surface.
   */
  function mountAudioSplitEditor(root, api, { actionName = "inspector_update_audio_split" } = {}) {
      /**
       * Query one Audio Split control inside the mounted surface.
       *
       * @param {string} selector - CSS selector for the desired control.
       * @returns {Element|null} Matching control or null.
       */
      const read = (selector) => root.querySelector(selector);
      const audioPath = read("[data-audio-split-audio-path]");
      const chunkDurationSec = read("[data-audio-split-chunk-duration-sec]");
      const maxChunkSizeMb = read("[data-audio-split-max-chunk-size-mb]");
      const workDir = read("[data-audio-split-work-dir]");
      const experimentalAudioFilterEnabled = read("[data-audio-split-experimental-audio-filter-enabled]");
      const experimentalAudioFilter = read("[data-audio-split-experimental-audio-filter]");
      const applyButton = read("[data-audio-split-apply]") || read("[data-block-apply]");
      let dirty = false;

      /**
       * Collect Audio Split form values into the block action payload.
       *
       * @returns {object} Current block configuration values.
       */
      const values = () => ({
        audio_path: audioPath?.value || "",
        chunk_duration_sec: chunkDurationSec?.value || "",
        max_chunk_size_mb: maxChunkSizeMb?.value || "",
        work_dir: workDir?.value || "",
        experimental_audio_filter_enabled: Boolean(experimentalAudioFilterEnabled?.checked),
        experimental_audio_filter: experimentalAudioFilter?.value || "",
      });

      /**
       * Mark Audio Split settings as changed and enable Apply.
       */
      const markDirty = () => {
        dirty = true;
        if (applyButton) {
          applyButton.disabled = false;
        }
      };

      /**
       * Persist the current Audio Split settings through the block action contract.
       */
      const apply = () => {
        if (!dirty) {
          return;
        }
        void api.applyAction(actionName, values()).then(() => {
          dirty = false;
          if (applyButton) {
            applyButton.disabled = true;
          }
        }).catch((error) => {
          api.log?.(`[error] Mise a jour Audio split impossible: ${error.message}`);
        });
      };

      experimentalAudioFilterEnabled?.addEventListener("change", markDirty);
      [audioPath, chunkDurationSec, maxChunkSizeMb, workDir, experimentalAudioFilter].forEach((element) => {
        element?.addEventListener("input", markDirty);
        element?.addEventListener("change", markDirty);
        element?.addEventListener("keydown", (event) => {
          if (event.key === "Enter" && element.tagName !== "TEXTAREA") {
            event.preventDefault();
            apply();
          }
        });
      });
      applyButton?.addEventListener("click", apply);
  }

  registry.audio_splitInspectorPanel = {
    /**
     * Mount the Audio Split inspector panel bindings.
     *
     * @param {HTMLElement} root - Mounted Audio Split inspector root.
     * @param {object} api - Generic block UI API exposing block actions.
     */
    mount(root, api) {
      mountAudioSplitEditor(root, api, { actionName: "inspector_update_audio_split" });
    },
  };

  registry.audio_split = {
    /**
     * Mount the Audio Split modal bindings using the modal update action.
     *
     * @param {HTMLElement} root - Mounted Audio Split modal root.
     * @param {object} api - Generic block UI API exposing block actions.
     */
    mount(root, api) {
      mountAudioSplitEditor(root, api, { actionName: "modal_update_audio_split" });
    },
  };
})();
