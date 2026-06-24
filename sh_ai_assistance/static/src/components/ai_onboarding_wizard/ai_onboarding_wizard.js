import { Component, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class AiOnboardingWizard extends Component {
    static template = "sh_ai_assistance.AiOnboardingWizard";
    static props = {
        onComplete: { type: Function },
        onSkip: { type: Function },
        onboardingState: { type: Object },
    };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");

        this.state = useState({
            // Current screen: 'welcome' | 'provider' | 'api_key' | 'default_model' | 'style' | 'success'
            currentScreen: 'welcome',
            // Data collected during onboarding
            selectedProvider: null,
            apiKey: '',
            selectedModelId: null,
            selectedStyle: 'precise',
            // Available data from backend
            providers: [],
            providerModels: [],
            // UI state
            isVerifying: false,
            verifyError: '',
            verifySuccess: '',
            isAnimating: false,
        });

        // Load provider data
        this.loadProviders();
    }

    async loadProviders() {
        try {
            const providers = await this.orm.call("sh.ai.llm", "get_onboarding_providers", []);
            this.state.providers = providers;
        } catch (error) {
            console.error("Failed to load providers:", error);
        }
    }

    // =====================================================================
    //  NAVIGATION
    // =====================================================================

    async goToScreen(screen) {
        this.state.isAnimating = true;
        // Small delay for smooth transition
        await new Promise(r => setTimeout(r, 150));
        this.state.currentScreen = screen;
        this.state.isAnimating = false;
    }

    startSetup() {
        this.goToScreen('provider');
    }

    skipSetup() {
        this.props.onSkip();
    }

    // =====================================================================
    //  STEP 1: CHOOSE PROVIDER
    // =====================================================================

    selectProvider(provider) {
        this.state.selectedProvider = provider;
        this.state.apiKey = '';
        this.state.verifyError = '';
        this.state.verifySuccess = '';
        // Pre-load models for this provider
        this.state.providerModels = provider.models || [];
        // Auto-select the default model, or the first one
        const defaultModel = this.state.providerModels.find(m => m.is_default);
        this.state.selectedModelId = defaultModel ? defaultModel.id : (this.state.providerModels[0]?.id || null);
    }

    get isProviderSelected() {
        return !!this.state.selectedProvider;
    }

    get providerIcon() {
        if (!this.state.selectedProvider) return '';
        const company = this.state.selectedProvider.company.toLowerCase();
        if (company === 'google') return '✦';
        if (company === 'openai') return '◆';
        if (company === 'anthropic') return '◈';
        if (company === 'deepseek') return '◉';
        return '●';
    }

    async confirmProvider() {
        if (!this.state.selectedProvider) return;
        try {
            await this.orm.call("sh.ai.llm", "save_onboarding_provider", [
                this.state.selectedProvider.company,
            ]);
            this.goToScreen('api_key');
        } catch (error) {
            console.error("Failed to save provider:", error);
        }
    }

    // =====================================================================
    //  STEP 2: API KEY
    // =====================================================================

    onApiKeyInput(ev) {
        this.state.apiKey = ev.target.value;
        // Clear any previous errors when user types
        this.state.verifyError = '';
        this.state.verifySuccess = '';
    }

    get apiKeyHelpUrl() {
        if (!this.state.selectedProvider) return '#';
        const company = this.state.selectedProvider.company.toLowerCase();
        if (company === 'google') return 'https://aistudio.google.com/apikey';
        if (company === 'openai') return 'https://platform.openai.com/api-keys';
        if (company === 'openrouter') return 'https://openrouter.ai/settings/keys';
        if (company === 'anthropic') return 'https://console.anthropic.com/settings/keys';
        if (company === 'deepseek') return 'https://platform.deepseek.com/api_keys';
        return '#';
    }

    get canVerifyKey() {
        return this.state.apiKey.trim().length > 10 && !this.state.isVerifying;
    }

    async verifyAndSaveApiKey() {
        if (!this.canVerifyKey) return;

        this.state.isVerifying = true;
        this.state.verifyError = '';
        this.state.verifySuccess = '';

        try {
            const result = await this.orm.call("sh.ai.llm", "save_onboarding_api_key", [
                this.state.selectedProvider.company,
                this.state.apiKey.trim(),
            ]);

            if (result.success) {
                this.state.verifySuccess = result.message || 'API key verified!';
                if (Array.isArray(result.models)) {
                    this.state.providerModels = result.models;
                    this.state.selectedProvider = {
                        ...this.state.selectedProvider,
                        models: result.models,
                    };
                    const defaultModel = result.models.find(m => m.is_default);
                    this.state.selectedModelId = defaultModel ? defaultModel.id : (result.models[0]?.id || null);
                }
                // Wait a moment to show success, then move to next screen
                await new Promise(r => setTimeout(r, 1200));
                this.goToScreen('default_model');
            } else {
                this.state.verifyError = result.message || 'Verification failed.';
            }
        } catch (error) {
            console.error("API key verification failed:", error);
            this.state.verifyError = 'An unexpected error occurred. Please try again.';
        } finally {
            this.state.isVerifying = false;
        }
    }

    // =====================================================================
    //  STEP 3: DEFAULT MODEL
    // =====================================================================

    selectModel(modelId) {
        this.state.selectedModelId = modelId;
    }

    get selectedModelName() {
        const model = this.state.providerModels.find(m => m.id === this.state.selectedModelId);
        return model ? model.name : '';
    }

    get isOpenRouterProvider() {
        return (this.state.selectedProvider?.company || '').toLowerCase() === 'openrouter';
    }

    onOpenRouterModelChange(ev) {
        const selectedId = parseInt(ev.target.value, 10);
        this.state.selectedModelId = Number.isNaN(selectedId) ? null : selectedId;
    }

    async confirmDefaultModel() {
        if (!this.state.selectedModelId) return;
        try {
            await this.orm.call("sh.ai.llm", "save_onboarding_default_model", [
                this.state.selectedModelId,
            ]);
            this.goToScreen('style');
        } catch (error) {
            console.error("Failed to save default model:", error);
        }
    }

    // =====================================================================
    //  STEP 4: RESPONSE STYLE
    // =====================================================================

    selectStyle(style) {
        this.state.selectedStyle = style;
    }

    async confirmStyle() {
        try {
            await this.orm.call("sh.ai.llm", "save_onboarding_style", [
                this.state.selectedStyle,
            ]);
            this.goToScreen('success');
        } catch (error) {
            console.error("Failed to save style:", error);
        }
    }

    // =====================================================================
    //  SUCCESS SCREEN
    // =====================================================================

    async openChat() {
        try {
            await this.orm.call("sh.ai.llm", "complete_onboarding", []);
        } catch (error) {
            console.error("Failed to complete onboarding:", error);
        }
        this.props.onComplete();
    }
}
