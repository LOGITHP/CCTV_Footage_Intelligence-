"""Clean duplicate live-config sections in frontend/index.html keeping exactly one."""

import re

file_path = "frontend/index.html"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Find the live-config section block
pattern = r"(\s*<!-- 2\.5 LIVE STREAM CONFIGURATION VIEW[\s\S]*?</section>)"
matches = re.findall(pattern, content)
print(f"Found {len(matches)} occurrences of live-config section block")

if len(matches) > 1:
    single_block = matches[0]
    # Remove all occurrences
    cleaned = re.sub(pattern, "", content)
    # Insert exactly once right after </section> of camera view
    cam_end = '</section>\n\n        <!-- 3. CAMERA STREAMS VIEW'
    if cam_end in cleaned:
        cleaned = cleaned.replace(cam_end, '</section>\n' + single_block + '\n\n        <!-- 3. CAMERA STREAMS VIEW')
    else:
        # Fallback insertion
        pos = cleaned.find('data-view-panel="camera"')
        if pos != -1:
            end_pos = cleaned.find('</section>', pos) + len('</section>')
            cleaned = cleaned[:end_pos] + '\n' + single_block + cleaned[end_pos:]
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    print("Cleaned up duplicates! Kept exactly one section.")
else:
    print("No duplicate sections found or pattern mismatch.")

