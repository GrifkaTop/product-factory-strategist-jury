"""Public errors shared by the local strategist tools."""


class StrategistError(Exception):
    """Base class for expected tool errors."""


class ValidationError(StrategistError):
    """Input does not satisfy the tool's contract."""


class IntegrityError(StrategistError):
    """Persisted data cannot be trusted or safely committed."""


class CheckpointCommitError(IntegrityError):
    """Publishing a particular case revision failed at a known stage."""

    def __init__(self, case_id, revision, stage, cause):
        super().__init__(f"Checkpoint commit failed at {stage}; inspect latest before retrying: {cause}")
        self.case_id = case_id
        self.revision = revision
        self.stage = stage


class RevisionConflict(StrategistError):
    """A change was prepared against another revision."""
