// Sentence-transformers mean pooling with attention masking and L2 normalisation.
import * as ort from "onnxruntime-web/wasm";
import { Tokenizer } from "@huggingface/tokenizers";
import { readFile } from "node:fs/promises";
const directory = new URL("../data/minilm/", import.meta.url);
export const dimensions = 384;
ort.env.wasm.numThreads = 1;
let loading, ranking;
async function load(root = directory) {
  const [model, tokenizer, config] = await Promise.all(
    ["model_quantized.onnx", "tokenizer.json", "tokenizer_config.json"].map(
      (file) => readFile(new URL(file, root)),
    ),
  );
  return {
    session: await ort.InferenceSession.create(model, {
      executionProviders: ["wasm"],
    }),
    tokenizer: new Tokenizer(JSON.parse(tokenizer), JSON.parse(config)),
  };
}
async function run(session, rows) {
  const width = Math.min(192, Math.max(...rows.map((row) => row.ids.length)));
  const shape = [rows.length, width];
  const ids = new BigInt64Array(rows.length * width),
    mask = new BigInt64Array(ids.length),
    types = new BigInt64Array(ids.length);
  for (const [i, row] of rows.entries()) {
    for (let j = 0; j < Math.min(row.ids.length, width); j++) {
      const k = i * width + j;
      ids[k] = BigInt(row.ids[j]);
      mask[k] = 1n;
      types[k] = BigInt(row.token_type_ids?.[j] ?? 0);
    }
  }
  const tensor = (data) => new ort.Tensor("int64", data, shape);
  const outputs = await session.run({
    input_ids: tensor(ids),
    attention_mask: tensor(mask),
    token_type_ids: tensor(types),
  });
  return { outputs, width };
}
export async function relevance(guess, passages) {
  const { session, tokenizer } = await (ranking ??= load(
    new URL("reranker/", directory),
  ));
  const rows = passages.map((text) =>
    tokenizer.encode(guess, { text_pair: text, return_token_type_ids: true }),
  );
  const { outputs } = await run(session, rows);
  return Array.from(outputs.logits.data);
}
export async function encode(texts) {
  const { session, tokenizer } = await (loading ??= load());
  const rows = texts.map((text) => tokenizer.encode(text));
  const { outputs, width } = await run(session, rows);
  const hidden = outputs.last_hidden_state.data;
  return rows.map((row, i) => {
    const v = new Float32Array(dimensions);
    for (let j = 0; j < Math.min(width, row.ids.length); j++) {
      const offset = (i * width + j) * dimensions;
      for (let d = 0; d < dimensions; d++) v[d] += hidden[offset + d];
    }
    const norm = Math.sqrt(dot(v, v)) || 1;
    for (let d = 0; d < dimensions; d++) v[d] /= norm;
    return v;
  });
}
export function dot(a, b) {
  let score = 0;
  for (let i = 0; i < a.length; i++) score += a[i] * b[i];
  return score;
}
