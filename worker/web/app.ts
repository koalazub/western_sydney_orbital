// Own this page before importing code that attaches input handlers or creates a canvas.
import { claimPageStartup } from "./graphics.ts";
import { initializeStartupRecovery, showStartupFailure } from "./startup-errors.ts";

if (claimPageStartup()) {
  initializeStartupRecovery();
  void import("./app-runtime.ts").catch(error => showStartupFailure(error));
}
