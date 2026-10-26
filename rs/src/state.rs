//! The one place that decides what state the app is in.
//!
//! `apply` is pure: it takes the current state and an event and returns the next
//! state, or `Rejected` when the event does not make sense right now. The caller
//! turns a rejection into visible feedback (for example a "busy" flash on the bubble)
//! instead of silently ignoring the hotkey as the Python app does.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum State {
    Loading,
    Idle,
    Listening,
    Transcribing,
    Paused,
    Error(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    ModelReady,
    ModelFailed(String),
    /// Hotkey (or the bubble) asked to start dictating.
    StartListen,
    /// Hotkey asked to stop; audio is handed off for transcription.
    StopListen,
    /// Recording was shorter than the minimum; nothing to transcribe.
    TooShort,
    TranscriptionDone,
    Pause,
    Resume,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Rejected {
    /// The app is busy (transcribing, loading) so the request can't run now.
    Busy,
    /// The request is meaningless in this state (e.g. Stop while idle).
    Invalid,
}

pub fn apply(state: &State, ev: Event) -> Result<State, Rejected> {
    use Event as E;
    use State as S;
    match (state, ev) {
        (S::Loading, E::ModelReady) => Ok(S::Idle),
        (S::Loading, E::ModelFailed(msg)) => Ok(S::Error(msg)),
        (S::Loading, _) => Err(Rejected::Busy),

        (S::Idle, E::StartListen) => Ok(S::Listening),
        (S::Idle, E::Pause) => Ok(S::Paused),
        (S::Idle, _) => Err(Rejected::Invalid),

        (S::Listening, E::StopListen) => Ok(S::Transcribing),
        (S::Listening, E::TooShort) => Ok(S::Idle),
        (S::Listening, E::Pause) => Ok(S::Paused),
        (S::Listening, _) => Err(Rejected::Invalid),

        (S::Transcribing, E::TranscriptionDone) => Ok(S::Idle),
        (S::Transcribing, _) => Err(Rejected::Busy),

        (S::Paused, E::Resume) => Ok(S::Idle),
        (S::Paused, E::StartListen) => Err(Rejected::Invalid),
        (S::Paused, _) => Err(Rejected::Invalid),

        // A failed model stays failed until the app restarts.
        (S::Error(_), _) => Err(Rejected::Invalid),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn happy_path_dictation() {
        let s = apply(&State::Idle, Event::StartListen).unwrap();
        assert_eq!(s, State::Listening);
        let s = apply(&s, Event::StopListen).unwrap();
        assert_eq!(s, State::Transcribing);
        assert_eq!(apply(&s, Event::TranscriptionDone).unwrap(), State::Idle);
    }

    #[test]
    fn hotkey_during_transcription_is_busy_not_lost() {
        let r = apply(&State::Transcribing, Event::StartListen);
        assert_eq!(r, Err(Rejected::Busy));
    }

    #[test]
    fn short_recording_goes_back_to_idle() {
        let s = apply(&State::Listening, Event::TooShort).unwrap();
        assert_eq!(s, State::Idle);
    }

    #[test]
    fn pause_then_resume() {
        let s = apply(&State::Idle, Event::Pause).unwrap();
        assert_eq!(s, State::Paused);
        assert_eq!(apply(&s, Event::StartListen), Err(Rejected::Invalid));
        assert_eq!(apply(&s, Event::Resume).unwrap(), State::Idle);
    }

    #[test]
    fn pausing_mid_recording_is_allowed() {
        assert_eq!(apply(&State::Listening, Event::Pause).unwrap(), State::Paused);
    }

    #[test]
    fn loading_failure_is_terminal() {
        let s = apply(&State::Loading, Event::ModelFailed("cuda oom".into())).unwrap();
        assert_eq!(s, State::Error("cuda oom".into()));
        assert_eq!(apply(&s, Event::StartListen), Err(Rejected::Invalid));
    }

    #[test]
    fn stop_while_idle_is_invalid() {
        assert_eq!(apply(&State::Idle, Event::StopListen), Err(Rejected::Invalid));
    }
}
