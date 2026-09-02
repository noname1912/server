import sys
sys.stdout.reconfigure(encoding="utf-8")

from platforms.youtube import YouTubePlatform
from platforms.soundcloud import SoundCloudPlatform

print("=== YouTube Search Test ===")
yt = YouTubePlatform()
results = yt.search("son tung mtp", limit=3)
print(f"Got {len(results)} results")
for r in results:
    print(f"  id={r['id']}  dur={r['duration']}s  title={r['title'][:30]}")

print("\n=== SoundCloud Search Test ===")
sc = SoundCloudPlatform()
results2 = sc.search("lofi music", limit=3)
print(f"Got {len(results2)} results")
for r in results2:
    print(f"  id={str(r['id'])[:40]}  title={r['title'][:30]}")
