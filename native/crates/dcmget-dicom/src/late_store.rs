use std::time::Duration;

use crate::CancellationToken;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LateStorePolicy {
    pub initial_wait: Duration,
    pub quiet_period: Duration,
    pub maximum_wait: Duration,
    pub poll_interval: Duration,
}

impl Default for LateStorePolicy {
    fn default() -> Self {
        Self {
            initial_wait: Duration::from_secs(5),
            quiet_period: Duration::from_secs(3),
            maximum_wait: Duration::from_secs(30),
            poll_interval: Duration::from_millis(100),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LateStoreDecision {
    Wait(Duration),
    Drained,
    TimedOut,
    Cancelled,
}

/// Deterministic late-C-STORE state machine.
///
/// The DIMSE adapter supplies elapsed durations. Keeping time outside this
/// type makes the policy testable without sleeping and lets Tokio own timers.
#[derive(Debug, Clone)]
pub struct LateStoreTracker {
    policy: LateStorePolicy,
    expect_files: bool,
    last_completed_store: Option<Duration>,
}

impl LateStoreTracker {
    #[must_use]
    pub fn new(policy: LateStorePolicy, expect_files: bool) -> Self {
        Self {
            policy,
            expect_files,
            last_completed_store: None,
        }
    }

    pub fn observe_completed_store(&mut self, elapsed: Duration) {
        self.last_completed_store = Some(elapsed);
    }

    #[must_use]
    pub fn decision(
        &self,
        elapsed: Duration,
        cancellation: &CancellationToken,
    ) -> LateStoreDecision {
        if cancellation.is_cancelled() {
            return LateStoreDecision::Cancelled;
        }
        if let Some(last_store) = self.last_completed_store {
            if elapsed.saturating_sub(last_store) >= self.policy.quiet_period {
                return LateStoreDecision::Drained;
            }
        } else if !self.expect_files && elapsed >= self.policy.initial_wait {
            return LateStoreDecision::Drained;
        }
        if elapsed >= self.policy.maximum_wait {
            return LateStoreDecision::TimedOut;
        }
        let next_boundary = self
            .last_completed_store
            .map_or_else(
                || {
                    if self.expect_files {
                        self.policy.maximum_wait
                    } else {
                        self.policy.initial_wait
                    }
                },
                |last| last + self.policy.quiet_period,
            )
            .min(self.policy.maximum_wait);
        let until_boundary = next_boundary.saturating_sub(elapsed);
        LateStoreDecision::Wait(self.policy.poll_interval.min(until_boundary))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_policy_matches_existing_receiver_contract() {
        let policy = LateStorePolicy::default();
        assert_eq!(policy.initial_wait, Duration::from_secs(5));
        assert_eq!(policy.quiet_period, Duration::from_secs(3));
        assert_eq!(policy.maximum_wait, Duration::from_secs(30));
        assert_eq!(policy.poll_interval, Duration::from_millis(100));
    }

    #[test]
    fn unexpected_empty_result_drains_after_initial_wait() {
        let token = CancellationToken::new();
        let tracker = LateStoreTracker::new(LateStorePolicy::default(), false);
        assert!(matches!(
            tracker.decision(Duration::from_secs(4), &token),
            LateStoreDecision::Wait(_)
        ));
        assert_eq!(
            tracker.decision(Duration::from_secs(5), &token),
            LateStoreDecision::Drained
        );
    }

    #[test]
    fn expected_file_waits_and_then_requires_quiet_period() {
        let token = CancellationToken::new();
        let mut tracker = LateStoreTracker::new(LateStorePolicy::default(), true);
        assert_eq!(
            tracker.decision(Duration::from_secs(6), &token),
            LateStoreDecision::Wait(Duration::from_millis(100))
        );
        tracker.observe_completed_store(Duration::from_secs(7));
        assert!(matches!(
            tracker.decision(Duration::from_secs(9), &token),
            LateStoreDecision::Wait(_)
        ));
        assert_eq!(
            tracker.decision(Duration::from_secs(10), &token),
            LateStoreDecision::Drained
        );
    }

    #[test]
    fn timeout_and_cancellation_are_visible() {
        let token = CancellationToken::new();
        let tracker = LateStoreTracker::new(LateStorePolicy::default(), true);
        assert_eq!(
            tracker.decision(Duration::from_secs(30), &token),
            LateStoreDecision::TimedOut
        );
        token.cancel();
        assert_eq!(
            tracker.decision(Duration::from_secs(1), &token),
            LateStoreDecision::Cancelled
        );
    }
}
