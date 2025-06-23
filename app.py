from fastapi import FastAPI, UploadFile, File
from fastapi.responses import PlainTextResponse
import json
from collections import Counter, defaultdict

app = FastAPI()

def review_ai_json_content(posts):
    results = []
    issues = defaultdict(list)
    title_counter = Counter()
    slug_counter = Counter()
    missing_featured = 0
    missing_meta_keys = Counter()
    total = len(posts)

    important_meta = [
        'seo_title', 'meta_description', 'focus_keyword', 'custom_slug',
        '_yoast_wpseo_title', '_yoast_wpseo_metadesc'
    ]
    
    for post in posts:
        pid = post.get('ID')
        title = post.get('post_title', '')
        slug = post.get('post_name', '')
        content = post.get('post_content', '')
        excerpt = post.get('post_excerpt', '')
        featured = post.get('featured_image')
        meta = post.get('meta', {})
        
        post_issues = []

        if not title or len(title.strip()) < 5:
            post_issues.append("Title is missing or too short.")
        title_counter[title.lower().strip()] += 1

        if not slug:
            post_issues.append("Slug (post_name) is missing.")
        slug_counter[slug.lower().strip()] += 1

        if not content or len(content.strip()) < 20:
            post_issues.append("Content is missing or too short.")
        if not excerpt or len(excerpt.strip()) < 10:
            post_issues.append("Excerpt is missing or too short.")

        if not featured:
            post_issues.append("Missing featured image.")
            missing_featured += 1

        for key in important_meta:
            if key not in meta or not meta[key]:
                post_issues.append(f"Missing important meta field: {key}")
                missing_meta_keys[key] += 1

        if post_issues:
            issues[pid] = {
                "title": title,
                "permalink": post.get('permalink'),
                "issues": post_issues
            }

    duplicates = []
    for field, counter in [('title', title_counter), ('slug', slug_counter)]:
        for val, count in counter.items():
            if val and count > 1:
                duplicates.append(f"Duplicate {field}: {val} ({count} times)")

    summary = [
        f"Total posts reviewed: {total}",
        f"Posts missing featured images: {missing_featured} ({(missing_featured/total)*100:.1f}%)",
    ]
    for k, v in missing_meta_keys.items():
        summary.append(f"Posts missing meta '{k}': {v} ({(v/total)*100:.1f}%)")
    if duplicates:
        summary.append("Duplicates found:\n" + "\n".join(duplicates))

    output = "# AI Review Results\n\n"
    output += "## Summary\n" + "\n".join(summary) + "\n\n"
    if issues:
        output += "## Per-Post Issues\n"
        for pid, issue_data in issues.items():
            output += f"- **[{issue_data['title']}]({issue_data.get('permalink')}) (ID: {pid})**\n"
            for msg in issue_data["issues"]:
                output += f"  - {msg}\n"
    else:
        output += "No issues found. Your content is in great shape!\n"

    return output

@app.post("/review-json", response_class=PlainTextResponse)
async def review_json(file: UploadFile = File(...)):
    posts = json.load(file.file)
    markdown = review_ai_json_content(posts)
    return markdown
