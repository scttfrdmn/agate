"""Phase 1 — the identity broker + ABAC stack (design §13.1/§13.2, the crux).

What this stack stands up (all $0 idle — NO CLOCKS):
  * a Cognito **Identity Pool** federated to the campus IdP (SAML/OIDC). No User
    Pool — we hook into existing campus federation and avoid MAU cost (design §5).
  * the **authenticated IAM role**, deny-by-default, whose effective scope is the
    generated model-access + data-scope policies keyed on `agg:` principal tags.
  * a **permissions boundary** that hard-caps the role to Bedrock+S3+S3Vectors read
    surfaces, so no future policy edit can widen it past the ABAC intent.
  * the per-request **broker Lambda** that validates the IdP token, derives the four
    `agg:` tags (incl. the computed `agg:tier`), and assumes the authenticated role
    narrowed by them.

The model->tier map is GENERATED from agg.entitlements (single source of truth),
never written inline here.
"""

from __future__ import annotations

import aws_cdk as cdk
from agg.names import HANDLE, tag_key
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_cognito_identitypool as idpool,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from constructs import Construct
from infra.assets import pip_bundled_code
from policy.generate import data_scope_policy, model_access_policy

# Sentinel for federation config that must be supplied before deploy.
PLACEHOLDER = "PLACEHOLDER"


class IdentityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account

        # --- Permissions boundary -----------------------------------------
        # Hard ceiling created first so the role can reference it. Even a future
        # mis-edit of the role cannot grant anything beyond these services; the
        # ABAC tags narrow WITHIN this ceiling.
        boundary = iam.ManagedPolicy(
            self,
            "AuthenticatedBoundary",
            managed_policy_name=f"{HANDLE}-authenticated-boundary",
            document=iam.PolicyDocument.from_json(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "CeilingBedrockInvoke",
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                                "bedrock:Converse",
                                "bedrock:ConverseStream",
                            ],
                            "Resource": "*",
                        },
                        {
                            "Sid": "CeilingDataRead",
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:ListBucket",
                                "s3vectors:QueryVectors",
                                "s3vectors:GetVectors",
                            ],
                            "Resource": "*",
                        },
                        {
                            # Explicit deny of anything that could widen privilege
                            # or persist beyond the session.
                            "Sid": "CeilingDenyEscalation",
                            "Effect": "Deny",
                            "Action": [
                                "iam:*",
                                "sts:*",
                                "bedrock:CreateProvisionedModelThroughput",
                                "s3:PutObject",
                                "s3:DeleteObject",
                            ],
                            "Resource": "*",
                        },
                    ],
                }
            ),
        )

        # --- Authenticated role -------------------------------------------
        # Trusted by the broker Lambda's execution role (sts:AssumeRole with
        # session Tags). The Cognito Identity Pool also lists it as the
        # authenticated role for the (future) browser-direct refresh path.
        authenticated_role = iam.Role(
            self,
            "AuthenticatedRole",
            role_name=f"{HANDLE}-authenticated",
            assumed_by=iam.CompositePrincipal(
                # Cognito-federated principals (browser-direct refresh path).
                iam.FederatedPrincipal(
                    "cognito-identity.amazonaws.com",
                    conditions={
                        "StringEquals": {
                            "cognito-identity.amazonaws.com:aud": cdk.Token.as_string(
                                self.node.try_get_context("identity_pool_id") or PLACEHOLDER
                            )
                        },
                        "ForAnyValue:StringLike": {
                            "cognito-identity.amazonaws.com:amr": "authenticated"
                        },
                    },
                    assume_role_action="sts:AssumeRoleWithWebIdentity",
                ),
            ),
            description="agg: authenticated session role, narrowed by agg: ABAC session tags",
            max_session_duration=cdk.Duration.hours(1),
            permissions_boundary=boundary,
        )

        # The broker assumes this role WITH session tags, so the trust policy
        # must also allow sts:AssumeRole + sts:TagSession from the broker role.
        # (Added after the broker role exists, below.)

        # --- Generated ABAC policies (single source of truth) -------------
        model_policy_doc = model_access_policy(region=region, account=account)
        data_policy_doc = data_scope_policy()

        authenticated_role.attach_inline_policy(
            iam.Policy(
                self,
                "ModelAccess",
                document=iam.PolicyDocument.from_json(model_policy_doc),
            )
        )
        authenticated_role.attach_inline_policy(
            iam.Policy(
                self,
                "DataScope",
                document=iam.PolicyDocument.from_json(data_policy_doc),
            )
        )

        # --- Broker Lambda -------------------------------------------------
        # Bundles infra/ + agg/ + policy/ source AND PyJWT, so claims_to_tags and the
        # real token verifier (agg.jwt_verify) run in-Lambda (SEC-4).
        broker = lambda_.Function(
            self,
            "Broker",
            function_name=f"{HANDLE}-broker",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="infra.functions.broker.handler.handler",
            code=pip_bundled_code("agg", "infra", "policy"),
            timeout=cdk.Duration.seconds(10),
            memory_size=256,
            environment={
                "AGG_AUTHENTICATED_ROLE_ARN": authenticated_role.role_arn,
                "AGG_SESSION_DURATION_SECONDS": "900",
            },
            description="agg: claims -> scoped-STS credential broker (per-request, scales to zero)",
        )

        # The broker is allowed to assume the authenticated role AND tag the session.
        broker.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:TagSession"],
                resources=[authenticated_role.role_arn],
            )
        )
        # Reflect that on the role's trust policy.
        authenticated_role.assume_role_policy.add_statements(  # type: ignore[union-attr]
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[broker.grant_principal],
                actions=["sts:AssumeRole", "sts:TagSession"],
            )
        )

        # --- Cognito Identity Pool (federated, no User Pool) ---------------
        # SAML/OIDC providers are deploy-time config (campus IdP). Supply via
        # `-c saml_provider_arn=...` / `-c oidc_provider_url=...`; both default to
        # PLACEHOLDER and the pool is created with whatever is provided.
        saml_arn = self.node.try_get_context("saml_provider_arn")
        # OIDC providers are wired in Phase 4 alongside the real campus IdP.

        auth_providers = idpool.IdentityPoolAuthenticationProviders(
            saml_providers=([idpool.IdentityPoolProviderUrl.saml(saml_arn)] if saml_arn else None),
            open_id_connect_providers=None,  # wired in Phase 4 with a real OIDC provider
        )

        pool = idpool.IdentityPool(
            self,
            "IdentityPool",
            identity_pool_name=f"{HANDLE}",
            allow_unauthenticated_identities=False,
            authenticated_role=authenticated_role,
            authentication_providers=auth_providers if saml_arn else None,
        )

        # --- Outputs -------------------------------------------------------
        cdk.CfnOutput(self, "IdentityPoolId", value=pool.identity_pool_id)
        cdk.CfnOutput(self, "AuthenticatedRoleArn", value=authenticated_role.role_arn)
        cdk.CfnOutput(self, "BrokerFunctionName", value=broker.function_name)
        cdk.CfnOutput(
            self,
            "FederationStatus",
            value=("saml-configured" if saml_arn else f"{PLACEHOLDER}-no-idp-wired"),
        )

        self.authenticated_role = authenticated_role
        self.model_policy_doc = model_policy_doc
        self.data_policy_doc = data_policy_doc


# Convenience for the policy-simulation proof: expose the tag keys used.
TIER_TAG_KEY = tag_key("tier")
TENANT_TAG_KEY = tag_key("tenant")
