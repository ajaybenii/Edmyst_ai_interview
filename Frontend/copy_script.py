import re

with open('/Users/ajaybeniwal/Desktop/clone-ai-interview-audio/Frontend/index.html', 'r') as f:
    index_html = f.read()

with open('/Users/ajaybeniwal/Desktop/clone-ai-interview-audio/Frontend/design3.html', 'r') as f:
    design3_html = f.read()

# Extract script from index.html
script_match = re.search(r'<script>(.*?)</script>', index_html, re.DOTALL)
if script_match:
    script_content = script_match.group(1)
    
    # Adjust IDs to match design3.html and add null checks
    script_content = script_content.replace("'networkSpeed'", "'networkText'")
    script_content = script_content.replace(
        "document.getElementById('serverLoader').classList.toggle('active', show);",
        "const el = document.getElementById('serverLoader'); if(el) el.classList.toggle('active', show);"
    )
    script_content = script_content.replace(
        "document.getElementById('loaderStatus').textContent = text;",
        "const el = document.getElementById('loaderStatus'); if(el) el.textContent = text;"
    )
    script_content = script_content.replace(
        "document.getElementById('interviewComplete').classList.add('active');",
        "const el = document.getElementById('interviewComplete'); if(el) el.classList.add('active');"
    )
    
    # Replace script in design3.html
    design3_new = re.sub(
        r'<script>(.*?)</script>', 
        '<script>' + script_content + '</script>', 
        design3_html, 
        flags=re.DOTALL
    )
    
    with open('/Users/ajaybeniwal/Desktop/clone-ai-interview-audio/Frontend/design3.html', 'w') as f:
        f.write(design3_new)
    print("Successfully copied script to design3.html")
else:
    print("Could not find script block in index.html")
