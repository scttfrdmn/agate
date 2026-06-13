// One adapter from the broker's ScopedCredentials to the AWS SDK v3 credential
// shape, shared by every signed client (Bedrock, S3 Vectors, AgentCore, the Tier 1
// Function URL). Including `expiration` lets the SDK treat the creds as
// refresh-aware; it's a Date the SDK reads, never a secret stored anywhere.

import type { ScopedCredentials } from "./index";

export function toSdkCredentials(c: ScopedCredentials) {
  return {
    accessKeyId: c.accessKeyId,
    secretAccessKey: c.secretAccessKey,
    sessionToken: c.sessionToken,
    expiration: new Date(c.expiration),
  };
}
