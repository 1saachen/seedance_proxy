import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .accounts import BusinessPrincipal, require_business_key


PRESET_ID_PATTERN = r"^preset_[a-z0-9][a-z0-9_-]{0,63}$"
PRESET_ORIGIN = "asset://"
PresetId = Annotated[str, Field(pattern=PRESET_ID_PATTERN)]
router = APIRouter(prefix="/api/asset", tags=["Asset library"])
Principal = Annotated[BusinessPrincipal, Depends(require_business_key)]


class PresetAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    asset_type: Literal["Image", "Video", "Audio"]
    upstream_id: str = Field(pattern=r"^asset-[A-Za-z0-9_-]{1,122}$")


def is_preset_reference(value: str) -> bool:
    return value.startswith(PRESET_ORIGIN) and re.fullmatch(PRESET_ID_PATTERN, value.removeprefix(PRESET_ORIGIN)) is not None


def translate_preset(presets: dict[str, PresetAsset], reference: str, *, media_type: str) -> str:
    asset = presets.get(reference.removeprefix(PRESET_ORIGIN))
    if asset is None:
        raise HTTPException(404, {"code": "preset_not_found", "message": "未找到该预置素材"})
    if asset.asset_type != media_type:
        raise HTTPException(422, {"code": "asset_type_mismatch", "message": "预置素材类型不匹配"})
    return PRESET_ORIGIN + asset.upstream_id


@router.get("/presets")
def list_presets(request: Request, principal: Principal) -> dict:
    return {"items": [
        {"id": preset_id, "name": asset.name, "assetType": asset.asset_type, "reference": PRESET_ORIGIN + preset_id}
        for preset_id, asset in sorted(request.app.state.settings.ark_preset_assets.items())
    ]}
