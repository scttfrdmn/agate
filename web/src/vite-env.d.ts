/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_AWS_REGION?: string;
  readonly VITE_BROKER_URL?: string;
  readonly VITE_DEFAULT_MODEL_ID?: string;
  readonly VITE_VECTOR_BUCKET?: string;
  readonly VITE_AGENT_RUNTIME_ARN?: string;
  readonly VITE_COGNITO_DOMAIN?: string;
  readonly VITE_COGNITO_CLIENT_ID?: string;
  readonly VITE_ADMIN_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
