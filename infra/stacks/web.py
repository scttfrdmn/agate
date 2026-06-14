"""Web hosting (design §11, demo-readiness #40) — static SPA on S3 + CloudFront.

The SPA is a static bundle; it has no server. This stack serves `web/dist` from a
private S3 bucket fronted by CloudFront with Origin Access Control (the bucket is
not public — only CloudFront can read it). CloudFront has no fixed monthly fee, so
this honours NO CLOCKS: cost is per-request/per-GB only.

Build-time SPA config (broker URL, region, vector bucket, agent runtime ARN) is
inlined by Vite at `npm run build`; this stack just publishes the already-built
`web/dist`. If `web/dist` is absent, deployment is skipped with a clear note so
`cdk synth` still works pre-build.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from agate.names import HANDLE
from aws_cdk import (
    Stack,
)
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3deploy,
)
from constructs import Construct

_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


class WebStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Private origin bucket — no public access; CloudFront reads it via OAC.
        site_bucket = s3.Bucket(
            self,
            "SiteBucket",
            bucket_name=f"{HANDLE}-web-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,  # the SPA is a rebuildable artifact
            auto_delete_objects=True,
        )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                # The SPA hardening (CSP/SRI, no third-party scripts) is applied at
                # build; a response-headers policy can be attached here when tuned.
            ),
            # SPA client-side routing: serve index.html for 403/404 (deep links).
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403, response_http_status=200, response_page_path="/index.html"
                ),
                cloudfront.ErrorResponse(
                    http_status=404, response_http_status=200, response_page_path="/index.html"
                ),
            ],
            comment="agate static SPA",
        )

        # Publish the built SPA if present (pre-build synth still works without it).
        if _DIST.is_dir():
            s3deploy.BucketDeployment(
                self,
                "DeploySite",
                sources=[s3deploy.Source.asset(str(_DIST))],
                destination_bucket=site_bucket,
                distribution=distribution,
                distribution_paths=["/*"],  # invalidate cache on deploy
            )

        cdk.CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        cdk.CfnOutput(self, "SiteBucketName", value=site_bucket.bucket_name)
        cdk.CfnOutput(
            self,
            "BuildStatus",
            value="dist-published" if _DIST.is_dir() else "no-web/dist (run npm run build first)",
        )

        self.bucket = site_bucket
        self.distribution = distribution
