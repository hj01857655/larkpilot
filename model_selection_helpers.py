"""模型选择辅助（照 ZCode modelSelectionHelpers.ts 抄）。"""

BOT_NATIVE_MODEL_PROVIDER_PREFIX = "native:"


def resolve_task_model(model):
    """"default" 不是真实模型名，返回 None。"""
    if model and model != "default":
        return model
    return None


def get_native_model_provider_id(zcode_provider):
    return f"{BOT_NATIVE_MODEL_PROVIDER_PREFIX}{zcode_provider}"
