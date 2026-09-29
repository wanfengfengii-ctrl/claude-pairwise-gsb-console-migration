import assert from "node:assert/strict";
import test from "node:test";
import { rankOpenApiOperations, sampleValue } from "../scripts/recording_openapi.mjs";

test("builds a valid non-degenerate polygon sample from an OpenAPI reference", () => {
  const spec = {
    components: { schemas: {
      PolygonIn: {
        type: "object", required: ["exterior"], properties: {
          exterior: { type: "array", items: { type: "array", prefixItems: [{ type: "integer" }, { type: "integer" }] } },
          holes: { type: "array", items: { type: "array" } },
        },
      },
      Request: {
        type: "object", required: ["a", "b"], properties: {
          a: { type: "array", items: { $ref: "#/components/schemas/PolygonIn" } },
          b: { type: "array", items: { $ref: "#/components/schemas/PolygonIn" } },
          optional_note: { type: "string" },
        },
      },
    } },
  };
  const value = sampleValue({ $ref: "#/components/schemas/Request" }, spec);
  assert.deepEqual(value.a[0].exterior, [[0, 0], [10, 0], [10, 10], [0, 10]]);
  assert.deepEqual(value.b[0].exterior, [[0, 0], [10, 0], [10, 10], [0, 10]]);
  assert.equal("optional_note" in value, false);
});

test("builds distinct coordinates for required paths", () => {
  const value = sampleValue({
    type: "object", required: ["path"], properties: {
      path: { type: "array", items: { type: "array", prefixItems: [{ type: "integer" }, { type: "integer" }] } },
    },
  }, {});
  assert.deepEqual(value.path, [[-5, 5], [15, 5]]);
});

test("uses a stable future date for expiring resources", () => {
  assert.equal(sampleValue({ type: "string", format: "date-time" }, {}, 0, "expires_at"),
    "2099-01-01T00:00:00Z");
});

test("builds a valid minimum-length nucleotide sequence", () => {
  const value = sampleValue({
    type: "object", required: ["sequence"], properties: { sequence: { type: "string" } },
  }, {});
  assert.equal(value.sequence, "GAAAAAAAAAAAAAAAAAAC");
  assert.equal(value.sequence.length, 20);
  assert.match(value.sequence, /^[ACG]+$/);
});

test("keeps generated vertices inside their generated canvas", () => {
  const value = sampleValue({
    type: "object",
    required: ["width", "height", "rows", "cols", "rotation", "vertices"],
    properties: {
      width: { type: "integer", exclusiveMinimum: 0 },
      height: { type: "integer", exclusiveMinimum: 0 },
      rows: { type: "integer", exclusiveMinimum: 0 },
      cols: { type: "integer", exclusiveMinimum: 0 },
      rotation: { type: "integer", enum: [0, 90, 180, 270] },
      vertices: {
        type: "array", minItems: 1,
        items: {
          type: "object", required: ["x", "y"],
          properties: { x: { type: "integer" }, y: { type: "integer" } },
        },
      },
    },
  }, {});

  assert.equal(value.width, 100);
  assert.equal(value.height, 100);
  assert.equal(value.vertices.length, 2);
  assert.deepEqual(value.vertices, [{ x: 9, y: 9 }, { x: 11, y: 11 }]);
  assert.ok(value.vertices.every(({ x, y }) => x >= 0 && x < value.width && y >= 0 && y < value.height));
});

test("builds a safe switch migration plan instead of unrelated placeholder ids", () => {
  const spec = {
    components: { schemas: {
      PlanCreateIn: {
        type: "object", required: ["idempotency_key", "topology"],
        properties: {
          idempotency_key: { type: "string" },
          topology: { type: "object" },
        },
      },
    } },
  };
  const sample = sampleValue({ $ref: "#/components/schemas/PlanCreateIn" }, spec);
  assert.equal(sample.topology.ingresses[0], "s1");
  assert.deepEqual(sample.topology.switches.map((item) => item.id), ["s1", "s2"]);
  assert.equal(sample.topology.switches[0].new_next, "DELIVER");
});

test("builds a consistent family phasing request", () => {
  const sample = sampleValue({
    type: "object", required: ["markers", "father", "mother", "children"], properties: {
      markers: { type: "array" }, father: { type: "array" },
      mother: { type: "array" }, children: { type: "array" },
    },
  }, {});
  assert.equal(sample.markers.length, 3);
  assert.equal(sample.father.length, sample.markers.length);
  assert.equal(sample.children[0].genotypes.length, sample.markers.length);
});

test("builds a valid linearizability history", () => {
  const sample = sampleValue({
    type: "object", required: ["initial_value", "operations"], properties: {
      initial_value: { type: "integer" }, operations: { type: "array" },
    },
  }, {});
  assert.deepEqual(sample.operations.map((operation) => operation.type), ["write", "read"]);
  assert.ok(sample.operations[0].respond < sample.operations[1].invoke);
  assert.equal(sample.operations[0].value, sample.operations[1].value);
});

test("prefers a root create operation without unresolved identifier dependencies", () => {
  const spec = {
    paths: {
      "/runs": { post: { requestBody: { content: { "application/json": {
        schema: { $ref: "#/components/schemas/CreateRun" },
      } } } } },
      "/pipelines": { post: { requestBody: { content: { "application/json": {
        schema: { $ref: "#/components/schemas/CreatePipeline" },
      } } } } },
    },
    components: { schemas: {
      CreateRun: {
        type: "object", required: ["pipeline_id", "seal_id", "idempotency_key"],
        properties: { pipeline_id: { type: "string" }, seal_id: { type: "string" }, idempotency_key: { type: "string" } },
      },
      CreatePipeline: { type: "object", properties: { name: { type: "string" } } },
    } },
  };
  assert.equal(rankOpenApiOperations(spec)[0].path, "/pipelines");
});

test("root status endpoint cannot stand in for a business API demo", () => {
  const spec = { paths: {
    "/": { get: { responses: { 200: {} } } },
    "/health": { get: { responses: { 200: {} } } },
    "/api/v1/deconvolve": { post: { responses: { 200: {} } } },
  } };
  assert.deepEqual(rankOpenApiOperations(spec).map((item) => item.path), ["/api/v1/deconvolve"]);
});
