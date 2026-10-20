import {defineConfig} from "@playwright/test";

export default defineConfig({
  testDir: "./tests/browser",
  timeout: 30000,
  workers: 1,
  use: {
    baseURL: process.env.SCI_BROWSER_URL || "http://localhost:8080",
    headless: true,
    viewport: {width: 1440, height: 1000},
    launchOptions: process.env.SCI_CHROMIUM_PATH ? {executablePath: process.env.SCI_CHROMIUM_PATH} : {},
  },
  reporter: "list",
});
