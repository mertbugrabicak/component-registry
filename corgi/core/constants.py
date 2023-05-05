"""
    model constants
"""
import re
from typing import Union

from django.db.models import Q

CONTAINER_DIGEST_FORMATS = (
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
CONTAINER_REPOSITORY = "registry.redhat.io"
CORGI_PRODUCT_TAXONOMY_VERSION = "v1"
CORGI_COMPONENT_TAXONOMY_VERSION = "v1"

# Map MPTT node levels in our product taxonomy to model names as defined in models.py
NODE_LEVEL_MODEL_MAPPING = {
    0: "product",
    1: "product_version",
    2: "product_stream",
    3: "product_variant",
    4: "channel",
}

# Map model names as defined in models.py to MPTT node levels in our product taxonomy
# "product_version" -> "ProductVersion"
MODEL_NODE_LEVEL_MAPPING = {
    value.title().replace("_", ""): key for key, value in NODE_LEVEL_MODEL_MAPPING.items()
}

# Take a model name like ProductVariant, make it lowercase
# then add an underscore to match the product_variants= filter in the API
MODEL_FILTER_NAME_MAPPING = {
    "Product": "products",
    "ProductVersion": "product_versions",
    "ProductStream": "product_streams",
    "ProductVariant": "product_variants",
}

# Filter on "root components": SRPMs or index container images
SRPM_CONDITION = Q(type="RPM", arch="src")
INDEX_CONTAINER_CONDITION = Q(type="OCI", arch="noarch")
ROOT_COMPONENTS_CONDITION = SRPM_CONDITION | INDEX_CONTAINER_CONDITION

# Regex for generating version_arr, release_arr and el_match fields
RELEASE_VERSION_DELIM_RE = re.compile("[.,\\-,_,\\+]")
EL_MATCH_RE = re.compile(".*el(\\d+)?[.,\\-,_]?(\\d+)?[.,\\-,_]?(\\d+)?(.*)")

# Epoch can be 0, "0", or missing,
EPOCH_TYPE = Union[int, str, None]
