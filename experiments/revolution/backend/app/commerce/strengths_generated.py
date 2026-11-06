# Generated from the frozen CityNexus domain YAML by tools/extract_commitment_strengths.py.
# Do not hand-invent or rename these source-domain values.
# Source file: D:\moodle\CityNexus\domain-model\citynexus-core-domain-model-v0.4.2.yaml

SOURCE_COMMITMENT_STRENGTHS: tuple[str, str, str, str, str] = (
    "C0_Flexible",
    "C1_Preferred",
    "C2_Reserved",
    "C3_Guaranteed",
    "C4_Anchored",
)

SOURCE_COMMITMENT_STRENGTH_BY_RANK = dict(enumerate(SOURCE_COMMITMENT_STRENGTHS))
