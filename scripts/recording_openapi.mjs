export function resolveSchema(schema, spec) {
  if (!schema?.$ref) return schema || {};
  return schema.$ref.slice(2).split("/").reduce((value, key) => value?.[key], spec) || {};
}

export function rankOpenApiOperations(spec) {
  const methods = ["post", "put", "patch", "get"];
  const candidates = [];
  for (const [path, definition] of Object.entries(spec.paths || {})) {
    // Root/docs endpoints prove that the server is reachable, not that a
    // business operation completed. Do not let them satisfy a demo.
    if (path === "/" || /^\/(?:docs|openapi\.json)\/?$/i.test(path)
        || /health|ready|live/i.test(path)) continue;
    for (const method of methods) {
      const operation = definition?.[method];
      if (!operation) continue;
      const content = operation.requestBody?.content?.["application/json"];
      const schema = resolveSchema(content?.schema, spec);
      const required = schema.required || [];
      const dependencyFields = required.filter((name) =>
        /(?:^|_)(?:id|key|token|reference)$|idempotency/i.test(name));
      const pathParameters = (path.match(/{[^}]+}/g) || []).length;
      const score = (content ? 100 : 0)
        + (method === "post" ? 20 : 0)
        + (pathParameters === 0 ? 60 : 0)
        - (dependencyFields.length * 35)
        - (pathParameters * 50)
        - path.length;
      candidates.push({ path, method, operation, content, spec, score });
    }
  }
  return candidates.sort((left, right) => right.score - left.score);
}

function stringSample(schema, fieldName) {
  const name = fieldName.toLowerCase();
  // ACG-only sequence is valid for both common DNA and RNA alphabets. Twenty
  // bases also clears the frequent minimum-length rule that custom validators
  // do not expose in OpenAPI.
  if (/sequence|nucleotide|rna|dna/.test(name)) return `G${"A".repeat(18)}C`;
  if (/sha|digest|checksum|hash/.test(name)) return "0".repeat(64);
  if (/file.*name|filename/.test(name)) return "demo.bin";
  if (/id$|_id$|uuid/.test(name)) return "demo-1";
  if (schema.format === "email") return "demo@example.com";
  if (schema.format === "date") return "2099-01-01";
  if (schema.format === "date-time") return "2099-01-01T00:00:00Z";
  const minimum = Math.max(1, Number(schema.minLength || 1));
  return "demo".padEnd(minimum, "x");
}

function boundedInteger(schema, preferred) {
  const minimum = Number.isFinite(Number(schema.minimum))
    ? Number(schema.minimum) : (Number.isFinite(Number(schema.exclusiveMinimum)) ? Number(schema.exclusiveMinimum) + 1 : 1);
  const maximum = Number.isFinite(Number(schema.maximum)) ? Number(schema.maximum) : preferred;
  return Math.max(minimum, Math.min(maximum, preferred));
}

function normalizeBoundedCoordinates(result, properties, spec) {
  const keyByCompactName = Object.fromEntries(
    Object.keys(properties).map((key) => [key.toLowerCase().replace(/[^a-z0-9]/g, ""), key]),
  );
  const widthKey = keyByCompactName.width || keyByCompactName.canvaswidth || keyByCompactName.imagewidth;
  const heightKey = keyByCompactName.height || keyByCompactName.canvasheight || keyByCompactName.imageheight;
  if (!widthKey || !heightKey || !(widthKey in result) || !(heightKey in result)) return result;

  const widthSchema = resolveSchema(properties[widthKey], spec);
  const heightSchema = resolveSchema(properties[heightKey], spec);
  result[widthKey] = boundedInteger(widthSchema, 100);
  result[heightKey] = boundedInteger(heightSchema, 100);
  const width = Number(result[widthKey]);
  const height = Number(result[heightKey]);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return result;

  for (const [field, value] of Object.entries(result)) {
    if (!/^(?:points?|vertices)$/i.test(field) || !Array.isArray(value)) continue;
    const desired = /^vertices$/i.test(field) ? Math.max(2, value.length) : Math.max(1, value.length);
    const maxX = Math.max(0, Math.trunc(width) - 1);
    const maxY = Math.max(0, Math.trunc(height) - 1);
    result[field] = Array.from({ length: desired }, (_, index) => ({
      ...(value[index] && typeof value[index] === "object" ? value[index] : {}),
      x: Math.min(maxX, index === 0 ? Math.min(9, maxX) : Math.min(11, maxX)),
      y: Math.min(maxY, index === 0 ? Math.min(9, maxY) : Math.min(11, maxY)),
    }));
  }
  return result;
}

export function sampleValue(inputSchema, spec, depth = 0, fieldName = "") {
  const schema = resolveSchema(inputSchema, spec);
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (schema.const !== undefined) return schema.const;
  if (schema.enum?.length) return schema.enum[0];
  if (schema.oneOf?.length || schema.anyOf?.length) {
    const options = schema.oneOf || schema.anyOf;
    const option = options.find((item) => resolveSchema(item, spec).type !== "null") || options[0];
    return sampleValue(option, spec, depth + 1, fieldName);
  }
  if (depth > 8) return null;
  if (schema.type === "object" || schema.properties) {
    const properties = schema.properties || {};
    if (properties.markers && properties.father && properties.mother && properties.children) {
      const father = resolveSchema(properties.father, spec);
      if (father.type === "array") {
        return {
          markers: ["rs01", "rs02", "rs03"],
          father: ["0/1", "0/1", "0/0"],
          mother: ["0/0", "0/1", "0/0"],
          children: [{ id: "proband", genotypes: ["0/0", "0/1", "0/0"] }],
        };
      }
      return {
        markers: ["m1", "m2"],
        father: { genotypes: ["AC", "TT"] },
        mother: { genotypes: ["GG", "AC"] },
        children: [{ name: "k", genotypes: ["AG", "TC"] }],
      };
    }
    if (properties.initial_value && properties.operations) {
      return {
        initial_value: 0,
        operations: [
          { id: "w", type: "write", value: 1, invoke: 0, respond: 2 },
          { id: "r", type: "read", value: 1, invoke: 3, respond: 4 },
        ],
      };
    }
    if (properties.L && properties.n && properties.distances) {
      return { L: 10, n: 5, distances: [2, 4, 7, 10, 2, 5, 8, 3, 6, 3] };
    }
    if (properties.idempotency_key && properties.topology) {
      return {
        idempotency_key: "recording-plan-1",
        topology: {
          switches: [
            { id: "s1", old_next: "s2", new_next: "DELIVER" },
            { id: "s2", old_next: "DELIVER", new_next: "DELIVER" },
          ],
          ingresses: ["s1"],
        },
      };
    }
    const required = new Set(schema.required || []);
    const result = Object.fromEntries(Object.entries(properties)
      .filter(([key, value]) => required.has(key)
        || value?.example !== undefined || value?.default !== undefined || value?.const !== undefined)
      .map(([key, value]) => [key, sampleValue(value, spec, depth + 1, key)]));
    return normalizeBoundedCoordinates(result, properties, spec);
  }
  if (schema.type === "array") {
    const name = fieldName.toLowerCase();
    if (/holes?/.test(name)) return [];
    if (/exterior|boundary|ring/.test(name)) return [[0, 0], [10, 0], [10, 10], [0, 10]];
    if (/path|polyline|transect/.test(name)) return [[-5, 5], [15, 5]];
    const count = Math.max(1, Number(schema.minItems || 1));
    return Array.from({ length: Math.min(count, 4) }, (_, index) => {
      if (schema.prefixItems?.length) {
        return schema.prefixItems.map((item, itemIndex) => {
          const value = sampleValue(item, spec, depth + 1, `${fieldName}_${itemIndex}`);
          return typeof value === "number" ? value + index : value;
        });
      }
      return sampleValue(schema.items || {}, spec, depth + 1, fieldName);
    });
  }
  if (schema.type === "integer" || schema.type === "number") {
    const name = fieldName.toLowerCase();
    if (/total.*size|file.*size|length|bytes/.test(name)) return Math.max(1024, Number(schema.minimum || 0));
    if (/chunk.*size/.test(name)) return Math.max(256, Number(schema.minimum || 0));
    return Math.max(1, Number(schema.minimum || 1));
  }
  if (schema.type === "boolean") return true;
  return stringSample(schema, fieldName);
}
