#!/usr/bin/env python3
"""Batch download books from a CSV of ASINs. Resumable - skips already downloaded/queued."""

import argparse
import csv
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from audible_downloader import db

def main():
    parser = argparse.ArgumentParser(description="Batch download audiobooks from CSV")
    parser.add_argument("--limit", "-n", type=int, help="Limit number of books to queue")
    parser.add_argument("--run", "-r", action="store_true", help="Run worker after queuing")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()

    csv_path = Path("keeps.csv")
    if not csv_path.exists():
        print(f"Error: {csv_path} not found")
        sys.exit(1)

    # Init DB
    db.init_db()

    # Get user (assuming single user from CLI auth import)
    users = db.get_all_users() if hasattr(db, 'get_all_users') else None
    if not users:
        # Fallback: query directly
        import sqlite3
        conn = sqlite3.connect("data/audible.db")
        cur = conn.execute("SELECT id, email FROM users LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            print("Error: No users in database. Run the web app and login first.")
            sys.exit(1)
        user_id, user_email = row
    else:
        user_id, user_email = users[0].id, users[0].email

    print(f"User: {user_email} (id={user_id})")

    # Read CSV
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        books = list(reader)

    print(f"CSV has {len(books)} books")

    # Get already downloaded books
    downloaded = {b.asin for b in db.get_user_books(user_id)}
    print(f"Already downloaded: {len(downloaded)}")

    # Get pending/in-progress jobs
    jobs = db.get_user_jobs(user_id)
    in_progress = {j.asin for j in jobs if j.status.value in ('pending', 'running')}
    completed_jobs = {j.asin for j in jobs if j.status.value == 'completed'}
    print(f"Jobs in progress: {len(in_progress)}")
    print(f"Jobs completed: {len(completed_jobs)}")

    # Figure out what needs downloading
    to_download = []
    for book in books:
        asin = book['asin']
        if asin in downloaded:
            continue  # Already have it
        if asin in in_progress:
            continue  # Job already queued
        if asin in completed_jobs:
            continue  # Job completed (might need manual check)
        to_download.append(book)

    print(f"Need to queue: {len(to_download)}")

    if not to_download:
        print("Nothing to do!")
        return

    # Apply limit
    if args.limit and args.limit < len(to_download):
        to_download = to_download[:args.limit]
        print(f"Limited to: {len(to_download)}")

    # Confirm
    print()
    print("Books to queue:")
    for book in to_download[:10]:
        print(f"  - {book['title'][:60]}")
    if len(to_download) > 10:
        print(f"  ... and {len(to_download) - 10} more")
    print()

    if not args.yes:
        response = input(f"Queue {len(to_download)} downloads? [y/N] ")
        if response.lower() != 'y':
            print("Aborted.")
            return

    # Queue downloads
    queued = 0
    skipped = 0
    for book in to_download:
        job = db.create_job(user_id, book['asin'], book['title'])
        if job:
            queued += 1
            print(f"Queued: {book['title'][:50]}")
        else:
            skipped += 1
            print(f"Skipped (already exists): {book['title'][:50]}")

    print()
    print(f"Done. Queued {queued}, skipped {skipped}")

    if args.run:
        print()
        print("Starting worker...")
        from audible_downloader.worker import worker
        try:
            worker.start()
            # Wait for jobs to complete
            import time
            while True:
                jobs = db.get_user_jobs(user_id)
                active = [j for j in jobs if j.status.value in ('pending', 'running')]
                if not active:
                    print("All jobs completed!")
                    break
                print(f"  {len(active)} jobs remaining...")
                time.sleep(10)
        except KeyboardInterrupt:
            print("\nStopping worker...")
            worker.stop()
    else:
        print()
        print("Run with --run to start processing, or:")
        print("  uv run python batch_download.py --run")


if __name__ == "__main__":
    main()
