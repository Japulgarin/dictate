//! Settings. Reads the same `config.toml` keys the Python app uses, so the
//! two can share one file during the migration.

use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(default)]
pub struct Config {
    pub model: String,
    pub device: String,
    pub language: String,
    pub hotkey: String,
    pub hotkey_mode: HotkeyMode,
    pub auto_paste: bool,
    pub bubble_hide_after_s: f32,
    pub silence_rms: f32,
    pub preroll_s: f32,
    pub terminal_apps: Vec<String>,
    /// Recording settings (not in the Python app).
    pub ffmpeg: String,
    pub record_fps: u32,
    pub record_encoder: String,
    pub record_mic: bool,
    /// Microphone name as ffmpeg lists it (dshow). When set, screen and mic are recorded by one ffmpeg
    /// process, so both share one clock and stay in sync. Empty = the separate cpal capture.
    pub mic_device: String,
    pub recordings_dir: String,
    /// Camera name as ffmpeg lists it; empty = first camera found.
    pub camera_device: String,
    /// Save mouse position and clicks next to each recording (used for zoom in the editor).
    pub record_cursor: bool,
    /// After each recording, also render a `.studio.mp4` with click zoom and a styled frame.
    pub record_studio: bool,
}

#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum HotkeyMode {
    Toggle,
    Hold,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            model: "parakeet-tdt-0.6b-v3".into(),
            device: "auto".into(),
            language: "auto".into(),
            hotkey: "ctrl+cmd".into(),
            hotkey_mode: HotkeyMode::Toggle,
            auto_paste: true,
            bubble_hide_after_s: 4.0,
            silence_rms: 0.01,
            preroll_s: 0.3,
            ffmpeg: "ffmpeg".into(),
            record_fps: 30,
            record_encoder: "libx264".into(),
            record_mic: true,
            mic_device: String::new(),
            recordings_dir: "recordings".into(),
            camera_device: String::new(),
            record_cursor: true,
            record_studio: true,
            terminal_apps: [
                "WindowsTerminal.exe",
                "OpenConsole.exe",
                "conhost.exe",
                "cmd.exe",
                "powershell.exe",
                "pwsh.exe",
                "wezterm-gui.exe",
                "alacritty.exe",
                "mintty.exe",
                "Hyper.exe",
                "Tabby.exe",
                "Warp.exe",
                "kitty.exe",
            ]
            .map(String::from)
            .to_vec(),
        }
    }
}

impl Config {
    pub fn parse(text: &str) -> anyhow::Result<Self> {
        Ok(toml::from_str(text)?)
    }

    /// Load from `path`. A missing file yields the defaults; a malformed one is an error
    /// so the user sees it instead of silently running with wrong settings.
    pub fn load(path: &Path) -> anyhow::Result<Self> {
        match std::fs::read_to_string(path) {
            Ok(text) => Self::parse(&text),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }

    /// Whether `exe` (a bare file name such as `WindowsTerminal.exe`) pastes with Ctrl+Shift+V.
    pub fn is_terminal(&self, exe: &str) -> bool {
        self.terminal_apps.iter().any(|t| t.eq_ignore_ascii_case(exe))
    }
}

/// Folder that holds the executable, falling back to the cwd.
pub fn app_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Default config location: `config.toml` next to the executable, falling back to the cwd.
pub fn default_path() -> PathBuf {
    let beside_exe = app_dir().join("config.toml");
    if beside_exe.exists() {
        beside_exe
    } else {
        PathBuf::from("config.toml")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_file_gives_defaults() {
        assert_eq!(Config::parse("").unwrap(), Config::default());
    }

    #[test]
    fn reads_python_config_keys() {
        let cfg = Config::parse(
            r#"
            model = "canary-1b-v2"
            hotkey_mode = "hold"
            auto_paste = false
            terminal_apps = ["Foo.exe"]
            "#,
        )
        .unwrap();
        assert_eq!(cfg.model, "canary-1b-v2");
        assert_eq!(cfg.hotkey_mode, HotkeyMode::Hold);
        assert!(!cfg.auto_paste);
        assert_eq!(cfg.terminal_apps, vec!["Foo.exe"]);
        assert_eq!(cfg.hotkey, "ctrl+cmd"); // untouched keys keep defaults
    }

    #[test]
    fn terminal_match_is_case_insensitive() {
        let cfg = Config::default();
        assert!(cfg.is_terminal("windowsterminal.exe"));
        assert!(!cfg.is_terminal("notepad.exe"));
    }

    #[test]
    fn reads_the_real_python_config_file() {
        // The Python app's config writes whole numbers like `bubble_hide_after_s = 4`.
        let text = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../app/config.toml")).unwrap();
        let cfg = Config::parse(&text).unwrap();
        assert_eq!(cfg.bubble_hide_after_s, 4.0);
        assert!(cfg.record_studio); // keys the Python file lacks keep their defaults
    }

    #[test]
    fn bad_hotkey_mode_is_an_error() {
        assert!(Config::parse(r#"hotkey_mode = "sometimes""#).is_err());
    }
}
