import { defineConfig } from '@playwright/test';
import direct from './playwright.direct.config.ts';
export default defineConfig({ ...direct, testMatch: ['direct-journey.spec.ts', 'agent-protected.spec.ts'] });
