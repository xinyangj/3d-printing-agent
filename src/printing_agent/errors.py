from __future__ import annotations


class PrintingAgentError(Exception):
    code = "printing_agent_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(PrintingAgentError):
    code = "not_found"


class ConflictError(PrintingAgentError):
    code = "conflict"


class InvalidTransitionError(ConflictError):
    code = "invalid_transition"


class ExternalServiceError(PrintingAgentError):
    code = "external_service_error"


class SourceCacheError(ExternalServiceError):
    code = "source_cache_error"


class PolicyViolationError(PrintingAgentError):
    code = "policy_violation"


class BudgetExhaustedError(PrintingAgentError):
    code = "budget_exhausted"


class ValidationError(PrintingAgentError):
    code = "validation_error"


class CandidateRejectedError(ValidationError):
    code = "candidate_rejected"

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        diagnostics: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.diagnostics = diagnostics or {}


class ConfigurationError(PrintingAgentError):
    code = "configuration_error"
