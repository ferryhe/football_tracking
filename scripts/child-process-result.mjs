import os from "node:os";

export function childExitCode(result) {
  if (Number.isInteger(result.status)) {
    return result.status;
  }
  if (typeof result.signal === "string") {
    const signalNumber = os.constants.signals[result.signal];
    if (Number.isInteger(signalNumber)) {
      return 128 + signalNumber;
    }
  }
  return 1;
}
