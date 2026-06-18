from __future__ import annotations

import asyncio
import os
from typing import Any, Protocol

from evaluation_service.app.models import EvaluationContext
from shared.idp_common.storage import upload_json


class EvaluationProvider(Protocol):
    name: str

    async def track(self, context: EvaluationContext) -> dict[str, Any]:
        ...


class ArtifactStoreProvider:
    name = "artifact_store"

    async def track(self, context: EvaluationContext) -> dict[str, Any]:
        key = f"jobs/{context.request.job_id}/evaluation/{context.evaluation_id}/metrics.json"
        payload = {
            "evaluation_id": context.evaluation_id,
            "job_id": context.request.job_id,
            "metrics": context.metrics,
            "parameters": context.parameters,
            "tags": context.tags,
        }
        artifact = await asyncio.to_thread(
            upload_json,
            context.settings,
            context.settings.evaluation_bucket,
            key,
            payload,
        )
        return {
            "provider": self.name,
            "status": "success",
            "artifact": artifact,
            "bucket": context.settings.evaluation_bucket,
            "key": key,
        }


class MLflowProvider:
    name = "mlflow"

    @staticmethod
    def _track_sync(context: EvaluationContext) -> dict[str, Any]:
        os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")
        import mlflow
        from mlflow.tracking import MlflowClient

        tracking_uri = context.settings.mlflow_tracking_uri
        experiment_name = str(getattr(context.settings, "evaluation_mlflow_experiment", "idp_pipeline"))
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment_name)
        client = MlflowClient(tracking_uri=tracking_uri)
        experiment = client.get_experiment_by_name(experiment_name)

        if experiment is not None:
            existing = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                filter_string=f"tags.evaluation_id = '{context.evaluation_id}'",
                max_results=1,
            )
            if existing:
                return {
                    "provider": MLflowProvider.name,
                    "status": "success",
                    "run_id": existing[0].info.run_id,
                    "idempotent_replay": True,
                    "tracking_uri": tracking_uri,
                    "experiment": experiment_name,
                }

        with mlflow.start_run(run_name=context.request.job_id) as run:
            mlflow.log_params(context.parameters)
            mlflow.log_metrics(context.metrics)
            mlflow.set_tags(context.tags)
            run_id = run.info.run_id

        return {
            "provider": MLflowProvider.name,
            "status": "success",
            "run_id": run_id,
            "idempotent_replay": False,
            "tracking_uri": tracking_uri,
            "experiment": experiment_name,
        }

    async def track(self, context: EvaluationContext) -> dict[str, Any]:
        return await asyncio.to_thread(self._track_sync, context)


PROVIDERS: dict[str, type[ArtifactStoreProvider] | type[MLflowProvider]] = {
    ArtifactStoreProvider.name: ArtifactStoreProvider,
    MLflowProvider.name: MLflowProvider,
}


def build_provider(name: str) -> EvaluationProvider:
    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise ValueError(f"unsupported evaluation provider: {name}")
    return provider_class()
