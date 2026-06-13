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
};
