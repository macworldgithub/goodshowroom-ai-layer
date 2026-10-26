import { describe, expect, it } from "vitest";
import sharedFixtures from "../../../contracts/fixtures.json";
import {
  ApiContractError,
  parseApiContract,
  type ContractName,
  type components,
} from "./api-contracts";

type ContractFixture = {
  name: string;
  model: ContractName;
  valid: boolean;
  json: string;
};

const contractNames = [
  "CreateChatRequest",
  "TurnRequest",
  "ChatResultEnvelope",
  "ErrorEnvelope",
] as const;

function isContractName(value: string): value is ContractName {
  return contractNames.some((name) => name === value);
}

const fixtures: ContractFixture[] = sharedFixtures.cases.map((fixture) => {
  if (!isContractName(fixture.model)) {
    throw new Error("Shared contract fixture uses an unsupported model.");
  }
  return { ...fixture, model: fixture.model };
});

function parseFixture(fixture: ContractFixture) {
  switch (fixture.model) {
    case "CreateChatRequest":
      return parseApiContract("CreateChatRequest", fixture.json);
    case "TurnRequest":
      return parseApiContract("TurnRequest", fixture.json);
    case "ChatResultEnvelope":
      return parseApiContract("ChatResultEnvelope", fixture.json);
    case "ErrorEnvelope":
      return parseApiContract("ErrorEnvelope", fixture.json);
    default:
      throw new Error("Unsupported contract fixture model.");
  }
}

function fixture(name: string, model: ContractName): ContractFixture {
  const found = fixtures.find((candidate) => candidate.name === name && candidate.model === model);
  if (!found) throw new Error("Required shared contract fixture is unavailable.");
  return found;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function mutateJson(raw: string, path: Array<string | number>, value: unknown): string {
  const decoded: unknown = JSON.parse(raw);
  if (path.length === 0) throw new Error("Mutation path is invalid.");

  let parent: unknown = decoded;
  for (const part of path.slice(0, -1)) {
    if (Array.isArray(parent) && typeof part === "number" && part in parent) {
      parent = parent[part];
    } else if (isRecord(parent) && typeof part === "string" && part in parent) {
      parent = parent[part];
    } else {
      throw new Error("Mutation path is invalid.");
    }
  }

  const finalPart = path[path.length - 1];
  if (Array.isArray(parent) && typeof finalPart === "number" && finalPart in parent) {
    parent[finalPart] = value;
  } else if (isRecord(parent) && typeof finalPart === "string") {
    parent[finalPart] = value;
  } else {
    throw new Error("Mutation path is invalid.");
  }

  return JSON.stringify(decoded);
}

function expectInvalid(model: ContractName, raw: string, httpStatus?: number): void {
  expect(() => parseApiContract(model, raw, httpStatus)).toThrowError(ApiContractError);
}

const zeroResult = fixture("saved_zero_result", "ChatResultEnvelope").json;
const sourceUnavailable = fixture("source_unavailable_error", "ErrorEnvelope").json;
const validTurn = fixture("turn_request", "TurnRequest").json;
const requestId = "7ed11ca9-26b2-44d3-96f4-351258801ed1";

describe("shared public API fixtures", () => {
  for (const contractFixture of fixtures) {
    it(`${contractFixture.valid ? "accepts" : "rejects"} ${contractFixture.name}`, () => {
      if (contractFixture.valid) {
        // This assignment checks that runtime results remain compatible with generated wire types.
        const parsed: components["schemas"][ContractName] = parseFixture(contractFixture);
        expect(parsed).toBeDefined();
      } else {
        expect(() => parseFixture(contractFixture)).toThrowError(ApiContractError);
      }
    });
  }
});

describe("successful response semantics", () => {
  it("accepts a valid zero result without marking counts as disclosed", () => {
    const result = parseApiContract("ChatResultEnvelope", zeroResult, 200);

    expect(result.data.response.status).toBe("complete");
    expect(result.data.response.facts[0]?.value).toBe(0);
    expect(result.data.response.coverage.counts_disclosed).toBe(false);
  });

  it("rejects a success response with a non-success HTTP status", () => {
    expectInvalid("ChatResultEnvelope", zeroResult, 201);
  });

  it("rejects a count that does not match the deterministic answer", () => {
    expectInvalid("ChatResultEnvelope", mutateJson(zeroResult, ["data", "response", "facts", 0, "value"], 1));
  });

  it("rejects a claim that does not match the answer", () => {
    expectInvalid(
      "ChatResultEnvelope",
      mutateJson(zeroResult, ["data", "response", "claims", 0, "text"], "Total leads: 1."),
    );
  });

  it("rejects evidence with a different metric version", () => {
    expectInvalid(
      "ChatResultEnvelope",
      mutateJson(zeroResult, ["data", "response", "evidence", 0, "metric_version"], 2),
    );
  });

  it("rejects a dataset row that does not match the verified fact", () => {
    expectInvalid(
      "ChatResultEnvelope",
      mutateJson(zeroResult, ["data", "response", "datasets", 0, "rows", 0, "value"], 1),
    );
  });

  it("rejects visuals that do not match the permitted result shape", () => {
    expectInvalid("ChatResultEnvelope", mutateJson(zeroResult, ["data", "response", "visuals"], []));
  });

  it("rejects an invalid source label", () => {
    expectInvalid(
      "ChatResultEnvelope",
      mutateJson(zeroResult, ["data", "response", "sources", 0, "label"], "Other records"),
    );
  });

  it("rejects HTML in the deterministic answer", () => {
    expectInvalid(
      "ChatResultEnvelope",
      mutateJson(zeroResult, ["data", "response", "answer"], "<img src=x onerror=alert(1)>"),
    );
  });

  it("rejects an unknown internal field", () => {
    expectInvalid("ChatResultEnvelope", mutateJson(zeroResult, ["data", "internal_token"], "synthetic"));
  });

  it("rejects a saved or temporary mode and persistence mismatch", () => {
    expectInvalid("ChatResultEnvelope", mutateJson(zeroResult, ["data", "mode"], "temporary"));
  });

  it("rejects a partial response with an inconsistent warning", () => {
    const partial = fixture("temporary_partial_result", "ChatResultEnvelope").json;
    expectInvalid("ChatResultEnvelope", mutateJson(partial, ["data", "response", "warnings", 0], "missing_data"));
  });

  it("rejects a calendar period that is not seven local days", () => {
    const sixDayPeriod = mutateJson(
      zeroResult,
      ["data", "response", "period", "end_local"],
      "2026-10-21T00:00:00+11:00",
    );
    expectInvalid(
      "ChatResultEnvelope",
      mutateJson(sixDayPeriod, ["data", "response", "period", "end_utc"], "2026-10-20T13:00:00Z"),
    );
  });

  it("rejects timestamps that do not match the declared timezone", () => {
    expectInvalid(
      "ChatResultEnvelope",
      mutateJson(zeroResult, ["data", "response", "period", "timezone"], "UTC"),
    );
  });
});

describe("error envelope semantics", () => {
  it("accepts a catalogued error with its approved HTTP status", () => {
    const parsed = parseApiContract("ErrorEnvelope", sourceUnavailable, 503);
    expect(parsed.error.code).toBe("source_unavailable");
  });

  it("rejects an error with an incorrect HTTP status", () => {
    expectInvalid("ErrorEnvelope", sourceUnavailable, 502);
  });

  it("rejects an error message that differs from the catalog", () => {
    expectInvalid(
      "ErrorEnvelope",
      mutateJson(sourceUnavailable, ["error", "message"], "The source failed for synthetic data."),
    );
  });

  it("rejects a retry flag that differs from the catalog", () => {
    expectInvalid("ErrorEnvelope", mutateJson(sourceUnavailable, ["error", "retryable"], true));
  });

  it("rejects commit state when the error catalog does not allow it", () => {
    expectInvalid("ErrorEnvelope", mutateJson(sourceUnavailable, ["error", "commit_state"], "unknown"));
  });
});

describe("strict raw JSON input", () => {
  it("rejects a Unicode whitespace-only message", () => {
    expectInvalid("TurnRequest", mutateJson(validTurn, ["message"], "\u0085"));
  });

  it("rejects duplicate keys that use different Unicode escapes", () => {
    const duplicateEscapedKey = String.raw`{"request_id":"${requestId}","request\u005fid":"${requestId}"}`;
    expectInvalid("CreateChatRequest", duplicateEscapedKey);
  });

  it("rejects an unpaired UTF-16 surrogate", () => {
    const unpaired = JSON.stringify({ request_id: requestId, message: String.fromCharCode(0xd800), expected_version: 0 });
    expectInvalid("TurnRequest", unpaired);
  });

  it("rejects a JSON document above the UTF-8 byte limit", () => {
    const oversized = `${fixture("create_chat_request", "CreateChatRequest").json}${" ".repeat(65_536)}`;
    expectInvalid("CreateChatRequest", oversized);
  });

  it("rejects an input above the node limit", () => {
    const largeArray = Array(20_000).fill("0").join(",");
    const manyNodes = `{"request_id":"${requestId}","message":"x","expected_version":0,"extra":[${largeArray}]}`;
    expectInvalid("TurnRequest", manyNodes);
  });
});
