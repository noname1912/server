"""platforms package"""
from .zingmp3 import ZingMP3Platform
from .soundcloud import SoundCloudPlatform
from .youtube import YouTubePlatform

PLATFORMS = {
    "zing": ZingMP3Platform(),
    "soundcloud": SoundCloudPlatform(),
    "youtube": YouTubePlatform(),
}
