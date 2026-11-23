// reserve(), server_run(), output() and memory are generated from Rust in the same module.
const inputEncoder = new TextEncoder();
const outputDecoder = new TextDecoder();
function invokeRust(payload) {
  const input = inputEncoder.encode(JSON.stringify(payload));
  const pointer = reserve(input.length);
  new Uint8Array(memory.buffer, pointer, input.length).set(input);
  const length = server_run();
  return JSON.parse(outputDecoder.decode(new Uint8Array(memory.buffer, output(), length)));
}
export default createDashboardWorker(invokeRust, publicAssets);

