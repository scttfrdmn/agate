/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_AWS_REGION?: string;
  readonly VITE_BROKER_URL?: string;
  readonly VITE_DEFAULT_MODEL_ID?: string;
  readonly VITE_VECTOR_BUCKET?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
