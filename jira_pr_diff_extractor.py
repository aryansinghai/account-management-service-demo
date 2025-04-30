#!/usr/bin/env python3
"""
Jira PR Diff Extractor

This script:
1. Extracts a Jira ticket ID from a provided URL
2. Gets Jira ticket details including acceptance criteria
3. Lists GitHub PRs
4. Finds PRs matching the Jira ticket ID
5. Saves the acceptance criteria and diff to separate files
6. Optionally pushes a code review to GitHub PR

Usage:
python jira_pr_diff_extractor.py <jira_url> [pull|push]

Commands:
pull  - Extract PR diff and criteria (default if no command specified)
push  - Submit code review from review file to GitHub PR

Environment Variables:
JIRA_EMAIL - Your Jira email address
JIRA_API_TOKEN - Your Jira API token

Example:
python jira_pr_diff_extractor.py https://eagleviewcorp.atlassian.net/browse/ACATS-4622
python jira_pr_diff_extractor.py https://eagleviewcorp.atlassian.net/browse/ACATS-4622 push
"""

import argparse
import json
import os
import re
import requests
import subprocess
import sys
import urllib.parse
from datetime import datetime

def extract_jira_ticket_id(url):
    """Extract the Jira ticket ID from a URL"""
    # Parse the URL
    parsed_url = urllib.parse.urlparse(url)
    
    # Extract the ticket ID from the path
    path_parts = parsed_url.path.split('/')
    for part in path_parts:
        # Look for typical Jira ticket pattern (letters-numbers)
        if re.match(r'^[A-Z]+-\d+$', part):
            return part
    
    # If we couldn't find a part matching the pattern, try the last part
    last_part = path_parts[-1] if path_parts else ""
    print(f"Could not find standard Jira ticket format. Using last path component: {last_part}")
    return last_part

def get_jira_details(ticket_id):
    """Get Jira ticket details using the API"""
    print(f"Fetching Jira details for {ticket_id}...")
    
    # Get credentials from environment variables
    jira_email = os.environ.get('JIRA_EMAIL')
    jira_api_token = os.environ.get('JIRA_API_TOKEN')
    
    if not jira_email or not jira_api_token:
        print("Error: JIRA_EMAIL and JIRA_API_TOKEN environment variables must be set")
        print("Example: export JIRA_EMAIL='your.email@eagleview.com'")
        print("         export JIRA_API_TOKEN='your-api-token'")
        sys.exit(1)
    
    # Form the API URL
    jira_url = f"https://eagleviewcorp.atlassian.net/rest/api/3/issue/{ticket_id}"
    
    try:
        response = requests.get(
            jira_url,
            auth=(jira_email, jira_api_token),
            headers={"Accept": "application/json"},
            params={"expand": "names,schema,renderedFields"}  # Request field names and schema
        )
        
        if response.status_code != 200:
            print(f"Error: Failed to get Jira details. Status code: {response.status_code}")
            print(f"Response: {response.text}")
            sys.exit(1)
        
        jira_data = response.json()
        return jira_data
    
    except requests.exceptions.RequestException as e:
        print(f"Error: Failed to connect to Jira API: {e}")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Failed to parse Jira API response as JSON")
        sys.exit(1)

def get_parent_ticket_id(jira_data):
    """Extract parent ticket ID from Jira data if it exists"""
    fields = jira_data.get('fields', {})
    
    # Check for parent field (for subtasks)
    if 'parent' in fields and fields['parent']:
        parent = fields['parent']
        parent_key = parent.get('key')
        if parent_key:
            print(f"Found parent ticket: {parent_key}")
            return parent_key
    
    # Check for epic link (for stories under epics)
    for field_name, field_value in fields.items():
        if (field_name.startswith('customfield_') and 
            isinstance(field_value, str) and 
            re.match(r'^[A-Z]+-\d+$', field_value)):
            print(f"Found potential epic link: {field_value}")
            return field_value
    
    return None

def extract_acceptance_criteria(jira_data):
    """Extract acceptance criteria from the dedicated Acceptance Criteria field in Jira tickets"""
    print("Extracting acceptance criteria...")
    
    # Get all fields and their names
    fields = jira_data.get('fields', {})
    field_names = jira_data.get('names', {})
    
    criteria = []
    
    # Look for dedicated Acceptance Criteria field first
    ac_field_id = None
    
    # Check for field names that might contain "acceptance criteria"
    for field_id, field_name in field_names.items():
        if "acceptance criteria" in field_name.lower():
            ac_field_id = field_id
            print(f"Found Acceptance Criteria field: {field_id} ({field_name})")
            break
    
    # EagleView ACATS tickets often have a custom field for Acceptance Criteria
    # Try to extract it directly if we found the field ID
    if ac_field_id and ac_field_id in fields:
        field_value = fields[ac_field_id]
        
        # Extract based on field type
        if isinstance(field_value, dict) and 'content' in field_value:
            # This is in Atlassian Document Format (ADF)
            text = convert_adf_to_text(field_value)
            if text:
                # Split by new lines and filter empty lines
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                criteria.extend(lines)
        elif isinstance(field_value, str):
            # Plain text field
            lines = [line.strip() for line in field_value.split('\n') if line.strip()]
            criteria.extend(lines)
        elif isinstance(field_value, list):
            # Could be a list of values
            for item in field_value:
                if isinstance(item, str):
                    criteria.append(item.strip())
                elif isinstance(item, dict) and 'value' in item:
                    criteria.append(item['value'].strip())
    
    # If we didn't find a dedicated field or it didn't have content,
    # look through custom fields that might contain acceptance criteria
    if not criteria:
        custom_fields = {k: v for k, v in fields.items() if k.startswith('customfield_')}
        
        # Search for fields that might contain acceptance criteria
        for field_id, field_value in custom_fields.items():
            # Skip empty fields or those already checked
            if not field_value or field_id == ac_field_id:
                continue
                
            # Check field value - for text fields or ADF fields
            if isinstance(field_value, dict) and 'content' in field_value:
                text = convert_adf_to_text(field_value)
                # Look for text that appears to be acceptance criteria
                if "acceptance criteria" in text.lower() or "must " in text.lower():
                    lines = []
                    # Try to extract bullet points or numbered items
                    for line in text.split('\n'):
                        line = line.strip()
                        # Look for bullet points, numbered lists, or checkbox patterns
                        if re.match(r'^\s*[\*\-\d+\.]\s+', line) or "[ ]" in line:
                            clean_line = re.sub(r'^\s*[\*\-\d+\.]\s+', '', line).replace("[ ]", "").strip()
                            if clean_line:
                                lines.append(clean_line)
                    
                    if lines:
                        print(f"Found potential acceptance criteria in field: {field_id}")
                        criteria.extend(lines)
                        break
    
    # If still no criteria found, fall back to description
    if not criteria:
        print("No dedicated Acceptance Criteria field found, looking in description...")
        
        # Get the description field
        description = fields.get('description', {})
        
        # Specifically look for a section called "Acceptance Criteria" in the description
        if isinstance(description, dict) and 'content' in description:
            description_text = convert_adf_to_text(description)
            
            # Try to find a section titled "Acceptance Criteria"
            ac_section_match = re.search(r'(?i)acceptance criteria.*?(?:\n\n|$)', description_text, re.DOTALL)
            if ac_section_match:
                ac_section = ac_section_match.group(0)
                # Extract bullet points
                bullet_points = re.findall(r'(?:\n|\A)\s*[\*\-]\s+(.*?)(?:\n|$)', ac_section)
                if bullet_points:
                    criteria.extend([bp.strip() for bp in bullet_points if bp.strip()])
            
            # If no section found, extract bullet points that might be acceptance criteria
            if not criteria:
                # Extract all bullet list items
                bullet_items = []
                
                def extract_bullets(content_list):
                    nonlocal bullet_items
                    for item in content_list:
                        # Check for bullet list items
                        if item.get('type') == 'bulletList' and 'content' in item:
                            for list_item in item.get('content', []):
                                if 'content' in list_item:
                                    text = ""
                                    # Extract text from each paragraph in the list item
                                    for paragraph in list_item.get('content', []):
                                        if paragraph.get('type') == 'paragraph' and 'content' in paragraph:
                                            for text_node in paragraph.get('content', []):
                                                if text_node.get('type') == 'text':
                                                    text += text_node.get('text', '')
                                    if text.strip():
                                        bullet_items.append(text.strip())
                        # Recursively check for nested content
                        elif 'content' in item:
                            extract_bullets(item.get('content'))
                
                # Start extracting bullets from the top level content
                if 'content' in description:
                    extract_bullets(description.get('content', []))
                
                # Filter for items that look like acceptance criteria
                for item in bullet_items:
                    # Include items with "[ ]" or that appear to be requirements 
                    if "[ ]" in item or item.startswith("System must") or "must" in item.lower():
                        # Clean up the criteria text
                        clean_item = item.replace("[ ]", "").strip()
                        if clean_item:
                            criteria.append(clean_item)
        
        # If we couldn't extract criteria, try a simpler regex approach on the description text
        if not criteria and description_text:
            # Look for bullet points that mention requirements
            criteria = re.findall(r"(?:^|\n)[*\-]\s*(.*?must.*?)(?:\n|$)", description_text, re.IGNORECASE)
            
            # If still no criteria, look for any bullet points
            if not criteria:
                criteria = re.findall(r"(?:^|\n)[*\-]\s*(.*?)(?:\n|$)", description_text)
    
    print(f"Found {len(criteria)} acceptance criteria items")
    return criteria

def convert_adf_to_text(adf_json):
    """Convert Atlassian Document Format (ADF) to plaintext"""
    text = ""
    
    def process_content(content_list, indent=0):
        nonlocal text
        for item in content_list:
            if item.get('type') == 'text':
                text += item.get('text', '')
            elif item.get('type') in ['heading', 'paragraph']:
                if item.get('content'):
                    process_content(item['content'], indent)
                text += "\n"
            elif item.get('type') == 'bulletList':
                text += "\n"
                if item.get('content'):
                    for list_item in item['content']:
                        if list_item.get('content'):
                            text += "* "
                            process_content(list_item['content'], indent + 2)
                            text += "\n"
            elif item.get('type') == 'listItem':
                if item.get('content'):
                    process_content(item['content'], indent)
    
    if adf_json and 'content' in adf_json:
        process_content(adf_json['content'])
    
    return text

def run_command(command):
    """Run a shell command and return its output"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {command}")
        print(f"Error details: {e}")
        print(f"STDERR: {e.stderr}")
        sys.exit(1)

def list_github_prs():
    """List all GitHub PRs and return as a list of dictionaries"""
    print("Listing GitHub PRs...")
    
    # Get PR list in JSON format for easier parsing
    command = "gh pr list --json number,title,headRefName,url"
    output = run_command(command)
    
    try:
        prs = json.loads(output)
        print(f"Found {len(prs)} PRs")
        return prs
    except json.JSONDecodeError:
        print(f"Error parsing PR list. Raw output: {output}")
        sys.exit(1)

def find_matching_pr(prs, ticket_id):
    """Find a PR that matches the Jira ticket ID in title or branch name"""
    print(f"Looking for PRs matching ticket ID: {ticket_id}")
    
    matching_prs = []
    for pr in prs:
        if (ticket_id in pr.get('title', '') or 
            ticket_id in pr.get('headRefName', '')):
            matching_prs.append(pr)
    
    if not matching_prs:
        print(f"No matching PRs found for ticket ID: {ticket_id}")
        sys.exit(1)
    
    if len(matching_prs) > 1:
        print(f"Found {len(matching_prs)} matching PRs:")
        for i, pr in enumerate(matching_prs):
            print(f"{i+1}. #{pr['number']} - {pr['title']} ({pr['headRefName']})")
        
        # Let user choose which PR to use
        choice = input("Enter the number of the PR to use (default: 1): ")
        try:
            index = int(choice) - 1 if choice.strip() else 0
            selected_pr = matching_prs[index]
        except (ValueError, IndexError):
            print(f"Invalid choice. Using the first matching PR.")
            selected_pr = matching_prs[0]
    else:
        selected_pr = matching_prs[0]
    
    print(f"Selected PR #{selected_pr['number']}: {selected_pr['title']}")
    return selected_pr

def get_pr_diff(pr_number):
    """Get the diff for a PR"""
    print(f"Getting diff for PR #{pr_number}...")
    command = f"gh pr diff {pr_number}"
    return run_command(command)

def create_acceptance_criteria_file(ticket_id, summary, criteria, output_file):
    """Create a file with acceptance criteria"""
    print(f"Creating acceptance criteria file: {output_file}...")
    
    with open(output_file, 'w') as f:
        # Title section
        f.write(f"# Acceptance Criteria: {ticket_id}\n\n")
        f.write(f"## Summary\n{summary}\n\n")
        
        # Acceptance Criteria section
        f.write("## Acceptance Criteria\n\n")
        if criteria:
            for i, criterion in enumerate(criteria, 1):
                f.write(f"{i}. [ ] {criterion}\n")
        else:
            f.write("No explicit acceptance criteria found in the ticket.\n")
        f.write("\n")
        
        # Timestamp
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"_Generated on {now}_\n")
    
    # Check if the file was created
    if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
        print(f"Successfully created acceptance criteria file: {output_file}")
        print(f"File size: {os.path.getsize(output_file)} bytes")
    else:
        print(f"Warning: Output file is empty or wasn't created")
    
    return output_file

def create_pr_diff_file(ticket_id, pr_number, pr_title, diff, output_file):
    """Create a file with PR diff"""
    print(f"Creating PR diff file: {output_file}...")
    
    with open(output_file, 'w') as f:
        # Title section
        f.write(f"# PR Diff: {ticket_id}\n\n")
        f.write(f"## PR Details\n")
        f.write(f"PR Number: #{pr_number}\n")
        f.write(f"PR Title: {pr_title}\n\n")
        
        # Code Changes section
        f.write("## Code Changes\n\n")
        f.write("```diff\n")
        f.write(diff)
        f.write("\n```\n\n")
        
        # Review Notes section
        f.write("## Review Notes\n\n")
        f.write("<!-- Add your review notes here -->\n\n")
        
        # Timestamp
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"_Generated on {now}_\n")
    
    # Check if the file was created
    if os.path.exists(output_file) and os.path.getsize(output_file) > 0:
        print(f"Successfully created PR diff file: {output_file}")
        print(f"File size: {os.path.getsize(output_file)} bytes")
    else:
        print(f"Warning: Output file is empty or wasn't created")
    
    return output_file

def submit_code_review(ticket_id, pr_number):
    """Submit a code review to GitHub PR"""
    review_file = f"{ticket_id}_code_review.md"
    
    if not os.path.exists(review_file):
        print(f"Error: Code review file {review_file} not found. Generate it with the 'pull' command first.")
        return False
    
    print(f"Submitting code review from {review_file} to PR #{pr_number}...")
    
    try:
        command = f"gh pr review {pr_number} -c -F {review_file}"
        output = run_command(command)
        print(f"Successfully submitted code review to PR #{pr_number}")
        print(output)
        return True
    except Exception as e:
        print(f"Error submitting code review: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Extract Jira ticket details, find matching PRs, create files, and optionally submit a code review")
    parser.add_argument("jira_url", help="Jira ticket URL")
    parser.add_argument("command", nargs="?", choices=["pull", "push"], default="pull", 
                        help="Command to execute: 'pull' to extract data (default), 'push' to submit code review")
    parser.add_argument("--criteria-file", help="Acceptance criteria output file name template (default: <ticket_id>_criteria.md)")
    parser.add_argument("--diff-file", help="PR diff output file name template (default: <ticket_id>_diff.md)")
    parser.add_argument("--use-parent", action="store_true", help="Use parent ticket for acceptance criteria")
    
    args = parser.parse_args()
    
    # Step 1: Extract Jira ticket ID
    ticket_id = extract_jira_ticket_id(args.jira_url)
    if not ticket_id:
        print(f"Could not extract Jira ticket ID from URL: {args.jira_url}")
        sys.exit(1)
    print(f"Extracted Jira ticket ID: {ticket_id}")
    
    # For push command, just submit the code review and exit
    if args.command == "push":
        # Need to get PR number first
        prs = list_github_prs()
        matching_pr = find_matching_pr(prs, ticket_id)
        pr_number = matching_pr['number']
        
        success = submit_code_review(ticket_id, pr_number)
        if success:
            print(f"Code review for {ticket_id} successfully submitted to PR #{pr_number}")
        else:
            print(f"Failed to submit code review for {ticket_id}")
        sys.exit(0 if success else 1)
    
    # Continue with pull command (default)
    # Step 2: Get Jira ticket details
    jira_data = get_jira_details(ticket_id)
    summary = jira_data.get('fields', {}).get('summary', 'No summary available')
    print(f"Ticket summary: {summary}")
    
    # Step 2.5: Check for parent ticket and get its details if needed
    parent_ticket_id = get_parent_ticket_id(jira_data)
    parent_jira_data = None
    
    if parent_ticket_id:  # Always use parent ticket if it exists
        print(f"Getting details for parent ticket: {parent_ticket_id}")
        parent_jira_data = get_jira_details(parent_ticket_id)
        parent_summary = parent_jira_data.get('fields', {}).get('summary', 'No summary available')
        print(f"Parent ticket summary: {parent_summary}")
    
    # Step 3: Extract acceptance criteria
    if parent_jira_data:
        print("Extracting acceptance criteria from parent ticket...")
        criteria = extract_acceptance_criteria(parent_jira_data)
        if not criteria:
            print("No acceptance criteria found in parent ticket, falling back to original ticket...")
            criteria = extract_acceptance_criteria(jira_data)
    else:
        criteria = extract_acceptance_criteria(jira_data)
    
    # Step 4: List GitHub PRs
    prs = list_github_prs()
    
    # Step 5: Find a matching PR
    matching_pr = find_matching_pr(prs, ticket_id)
    pr_number = matching_pr['number']
    pr_title = matching_pr['title']
    
    # Step 6: Get the PR diff
    diff = get_pr_diff(pr_number)
    
    # Step 7: Create the acceptance criteria file
    criteria_file = args.criteria_file if args.criteria_file else f"{ticket_id}_criteria.md"
    parent_info = f" (from {parent_ticket_id})" if parent_jira_data else ""
    create_acceptance_criteria_file(f"{ticket_id}{parent_info}", summary, criteria, criteria_file)
    
    # Step 8: Create the PR diff file
    diff_file = args.diff_file if args.diff_file else f"{ticket_id}_diff.md"
    create_pr_diff_file(ticket_id, pr_number, pr_title, diff, diff_file)
    
    # Create a code review template file if it doesn't exist
    code_review_file = f"{ticket_id}_code_review.md"
    if not os.path.exists(code_review_file):
        print(f"Creating code review template file: {code_review_file}...")
        with open(code_review_file, 'w') as f:
            f.write(f"# Code Review: {ticket_id} - {pr_title}\n\n")
            f.write("## Line-by-Line Review\n\n")
            f.write("**Line/Section**: `file.ext:linenum`\n")
            f.write("- **Issue**: Brief description of the issue\n")
            f.write("- **Impact**: How this affects the code/functionality\n")
            f.write("- **Recommendation**: Suggested fix\n\n")
            f.write("## Design Pattern Evaluation\n\n")
            f.write("<!-- Evaluate if design patterns would improve the code -->\n\n")
            f.write("## Complexity Violations\n\n")
            f.write("<!-- Note any complexity issues -->\n\n")
            f.write("## Overall Assessment\n\n")
            f.write("<!-- Summary of findings and key recommendations -->\n")
    
    print(f"Process completed successfully!")
    print(f"Jira Ticket: {ticket_id}")
    if parent_jira_data:
        print(f"Parent Ticket: {parent_ticket_id}")
    print(f"PR: #{pr_number} - {pr_title}")
    print(f"Acceptance criteria file: {criteria_file}")
    print(f"PR diff file: {diff_file}")
    print(f"Code review file: {code_review_file}")
    print("\nTo submit your code review:")
    print(f"1. Edit {code_review_file} to add your review")
    print(f"2. Run: python {sys.argv[0]} {args.jira_url} push")
    
    # Optional: open the files
    if sys.platform == 'darwin':  # macOS
        open_command = f"open {criteria_file} {diff_file} {code_review_file}"
    elif sys.platform.startswith('linux'):
        open_command = f"xdg-open {criteria_file} {diff_file} {code_review_file}"
    elif sys.platform == 'win32':
        open_command = f"start {criteria_file} && start {diff_file} && start {code_review_file}"
    else:
        open_command = None
    
    if open_command:
        try:
            subprocess.run(open_command, shell=True, check=False)
        except Exception:
            print(f"Note: Could not automatically open the files. Please open them manually.")

if __name__ == "__main__":
    main() 