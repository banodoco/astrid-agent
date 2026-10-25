/** Astrid metadata adapter; OMP owns registries and model-role persistence. */
import { writeFileSync } from "node:fs";
import { cfgDisabledProviders } from "@oh-my-pi/pi-coding-agent/config/model-settings";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import {
	getModelMatchPreferences,
	pickDefaultAvailableModel,
	resolveAllowedModels,
	resolveModelRoleValue,
} from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import { getOAuthProviders } from "@oh-my-pi/pi-ai/oauth";

type ListedModel = { id: string; name: string; selector: string; usable: true };
type ListedProvider = { id: string; name: string; signedIn: boolean; models: ListedModel[] };
type Snapshot = {
	schemaVersion: 1;
	currentModel: string | null;
	providers: ListedProvider[];
	_catalogModel: string | null;
	_hiddenProviderIds: string[];
	_needsSignInProviderIds: string[];
	_oauthProviderIds: string[];
};

async function makeSnapshot(ctx: any, settings: any): Promise<Snapshot> {
	const registry = ctx.modelRegistry;
	const allModels = registry.getAll();
	const enabledModels = (await resolveAllowedModels(registry, settings, getModelMatchPreferences(settings)))
		.filter((model: any) => registry.hasConfiguredAuth(model));
	const oauthProviders = getOAuthProviders();
	const oauthById = new Map(oauthProviders.map((provider: any) => [provider.id, provider]));
	const disabled = new Set<string>(cfgDisabledProviders.get(settings) ?? []);
	const hiddenProviderIds = new Set(disabled);
	for (const oauth of oauthProviders as any[]) {
		if (oauth.storeCredentialsAs && disabled.has(oauth.storeCredentialsAs)) hiddenProviderIds.add(oauth.id);
	}
	const providerIds = new Set<string>([
		...allModels.map((model: any) => model.provider),
		...registry.getDiscoverableProviders(),
		...oauthProviders.map((provider: any) => provider.id),
	]);
	const providers: ListedProvider[] = [...providerIds]
		.filter(id => {
			const oauth = oauthById.get(id) as any;
			return !disabled.has(id) && !(oauth?.storeCredentialsAs && disabled.has(oauth.storeCredentialsAs));
		})
		.sort((a, b) => a.localeCompare(b))
		.map(id => {
			const oauth = (oauthById.get(id) ?? oauthProviders.find((provider: any) => provider.storeCredentialsAs === id)) as any;
			const models = enabledModels
				.filter((model: any) => model.provider === id)
				.map((model: any) => ({ id: model.id, name: model.name, selector: `${model.provider}/${model.id}`, usable: true as const }))
			.sort((a: ListedModel, b: ListedModel) => a.id.localeCompare(b.id));
			const hasAuth = registry.authStorage.keys.source(id) !== undefined || oauthProviders.some((provider: any) =>
				(provider.id === id || provider.storeCredentialsAs === id) &&
				registry.authStorage.keys.source(provider.storeCredentialsAs ?? provider.id) !== undefined,
			);
			return { id, name: oauth?.name ?? id, signedIn: hasAuth, models };
		});

	const preferences = getModelMatchPreferences(settings);
	// The global role is the durable default shared by App and terminal launches,
	// even when that selector cannot currently resolve (for example, while signed out).
	const globalDefault = settings.getGlobalModelRole("default");
	const effectiveDefault = settings.getModelRole("default");
	const resolvedDefault = effectiveDefault
		? resolveModelRoleValue(effectiveDefault, enabledModels, { settings, matchPreferences: preferences }).model
		: undefined;
	const fallback = globalDefault || resolvedDefault
		? undefined
		: pickDefaultAvailableModel(enabledModels);
	const selected = resolvedDefault ?? fallback;
	return {
		schemaVersion: 1,
		currentModel: globalDefault ?? (selected ? `${selected.provider}/${selected.id}` : null),
		providers,
		_catalogModel: allModels[0] ? `${allModels[0].provider}/${allModels[0].id}` : null,
		_hiddenProviderIds: [...hiddenProviderIds].sort((a, b) => a.localeCompare(b)),
		_oauthProviderIds: oauthProviders.map((provider: any) => provider.id),
		_needsSignInProviderIds: [...providerIds].filter(id =>
			allModels.some((model: any) => model.provider === id) &&
			!allModels.some((model: any) => model.provider === id && registry.hasConfiguredAuth(model)),
		),
	};
}

export default function register(pi: any): void {
	pi.on("session_shutdown", async (_event: unknown, ctx: any) => {
		const resultPath = process.env.ASTRID_OMP_MODELS_RESULT;
		if (!resultPath) throw new Error("ASTRID_OMP_MODELS_RESULT is required");
		const settingsForRead = await Settings.loadReadOnly({ cwd: ctx.cwd });
		writeFileSync(resultPath, JSON.stringify(await makeSnapshot(ctx, settingsForRead)));
	});
}
