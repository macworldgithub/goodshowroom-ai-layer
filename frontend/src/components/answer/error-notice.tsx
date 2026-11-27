import type { components } from "../../lib/api-contracts";
import { Alert, AlertTitle } from "../ui/alert";

type ApiError = components["schemas"]["ApiError"];

const titles: Record<ApiError["code"], string> = {
  authentication_required: "Sign in required",
  session_expired: "Session expired",
  approval_required: "Approval required",
  access_denied: "Access denied",
  not_found: "Not found",
  method_not_allowed: "Action not allowed",
  version_conflict: "This chat changed",
  idempotency_conflict: "Request conflict",
  request_in_progress: "Still working",
  policy_changed: "Permissions changed",
  temporary_unavailable: "Temporary chat ended",
  payload_too_large: "Message too large",
  invalid_request: "Invalid request",
  rate_limited: "Please wait",
  model_failure: "The answer could not be prepared",
  response_invalid: "The answer could not be validated",
  source_unavailable: "Data unavailable",
  authorization_unavailable: "Access could not be verified",
  persistence_failed: "Save problem",
  cleanup_pending: "Cleanup pending",
  deadline_exceeded: "The request timed out",
  internal_error: "Something went wrong",
};

/**
 * A failed request. It shows the fixed catalog message and never a number or a
 * partial answer. Save state is stated only as far as the server confirmed it.
 */
export function ErrorNotice({ error }: { error: ApiError }) {
  const title = Object.hasOwn(titles, error.code) ? titles[error.code] : "Something went wrong";
  return (
    <Alert tone="danger" role="alert" data-state="error" data-code={error.code}>
      <AlertTitle>{title}</AlertTitle>
      <p>{error.message}</p>
      {error.commit_state === "not_committed" ? <p className="mt-1">Not saved.</p> : null}
      {error.commit_state === "unknown" ? <p className="mt-1">Save status unknown. Check the request status before you try again.</p> : null}
      {error.retryable ? <p className="mt-1">You can try again.</p> : null}
    </Alert>
  );
}
