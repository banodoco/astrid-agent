import { z } from 'zod';

const modelUsageSchema = z.strictObject({
  limits: z.array(z.strictObject({
    unit: z.enum(['percent', 'tokens', 'requests', 'usd', 'minutes', 'bytes', 'unknown']).optional(),
    used: z.number().optional(), limit: z.number().optional(), remaining: z.number().optional(),
    usedFraction: z.number().optional(), remainingFraction: z.number().optional(),
    durationMs: z.number().optional(), resetsAt: z.number().optional(),
    status: z.enum(['ok', 'warning', 'exhausted', 'unknown']).optional(),
  })),
});
export const modelCatalogSchema = z.strictObject({
  providers: z.array(z.strictObject({
    id: z.string().min(1), name: z.string().min(1), signedIn: z.boolean(),
    usable: z.boolean(), reason: z.enum(['needs_sign_in', 'no_available_models']).nullable().optional(),
    login: z.enum(['oauth', 'api-key', 'local']).optional(), recommended: z.boolean().optional(),
    models: z.array(z.strictObject({ id: z.string().min(1), name: z.string().min(1) })),
    usage: modelUsageSchema.optional(),
  })),
  currentModel: z.string().nullable(),
});
export type AcpModelCatalog = z.infer<typeof modelCatalogSchema>;
