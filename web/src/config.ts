// Build-time config. Vite inlines `import.meta.env.VITE_*` at build; nothing here
// is a secret — the broker URL and region are public, and credentials are vended
// at runtime by the broker (never embedded). See CLAUDE.md "No secrets in client code".

export interface AppConfig {
  region: string;
  brokerUrl: string;
  defaultModelId: string;
  // S3 Vectors bucket holding the per-tenant indexes (empty = RAG disabled).
  vectorBucketName: string;
  // AgentCore Runtime ARN for the agent path (Panel/Analyze). Empty = agent modes
  // disabled (the SPA shows Ask only).
  agentRuntimeArn: string;
  // Cognito Hosted-UI login (demo IdP). Empty domain = no login button; the SPA
  // falls back to a manually pasted `#idp_token=` in the hash.
  cognitoDomain: string;
  cognitoClientId: string;
  // Governed-access console API (agate-admin). Empty = admin view hidden.
  adminUrl: string;
}

const env = import.meta.env;

export const config: AppConfig = {
  region: env.VITE_AWS_REGION ?? "us-east-1",
  brokerUrl: env.VITE_BROKER_URL ?? "",
  // Default to an oss-tier model so an unconfigured demo can't accidentally
  // target a frontier model the session may not be entitled to.
  defaultModelId: env.VITE_DEFAULT_MODEL_ID ?? "openai.gpt-oss-20b-1:0",
  vectorBucketName: env.VITE_VECTOR_BUCKET ?? "",
  agentRuntimeArn: env.VITE_AGENT_RUNTIME_ARN ?? "",
  cognitoDomain: env.VITE_COGNITO_DOMAIN ?? "",
  cognitoClientId: env.VITE_COGNITO_CLIENT_ID ?? "",
  adminUrl: env.VITE_ADMIN_URL ?? "",
};
