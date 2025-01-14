"""
Analyze source code and add various comment related metrics for a project
As a side effect, export comment statistics for each project to temp/comment_data/

Author: He, Hao
"""

import pandas as pd
import os
import json
import argparse
import hashlib
import sys
import resource
import re
import multiprocessing
from termcolor import colored


def get_file_list(path, suffix):
    """
    Given a path, recursively retrieve and return all files with given suffix in the path
    """
    result = []
    for root, dirs, files in os.walk(path):
        for file in files:
            if file.endswith(suffix):
                result.append(os.path.join(root, file))
    return result


def chunk_reader(fobj, chunk_size=1024):
    """
    Generator that reads a file in chunks of bytes
    """
    while True:
        chunk = fobj.read(chunk_size)
        if not chunk:
            return
        yield chunk


def get_hash(filename, first_chunk_only=False, hash=hashlib.sha1):
    """
    Compute hash for a given file path
    """
    hashobj = hash()
    file_object = open(filename, 'rb')

    if first_chunk_only:
        hashobj.update(file_object.read(1024))
    else:
        for chunk in chunk_reader(file_object):
            hashobj.update(chunk)
    hashed = hashobj.digest()

    file_object.close()
    return hashed


def remove_duplicates(filelist):
    """
    Given a list of file paths, return the list with duplicate files removed
    """
    hashes_by_size = {}
    hashes_on_1k = {}
    hashes_full = {}
    result = []

    for path in filelist:
        try:
            # if the target is a symlink (soft one), this will
            # dereference it - change the value to the actual target file
            path = os.path.realpath(path)
            file_size = os.path.getsize(path)
        except (OSError,):
            # not accessible (permissions, etc) - pass on
            continue
        duplicate = hashes_by_size.get(file_size)
        if duplicate:
            hashes_by_size[file_size].append(path)
        else:
            # create the list for this file size
            hashes_by_size[file_size] = []
            hashes_by_size[file_size].append(path)

    # For all files with the same file size,
    # get their hash on the 1st 1024 bytes
    for __, files in hashes_by_size.items():
        if len(files) < 2:
            result.extend(files)
            continue    # this file size is unique

        for filename in files:
            try:
                small_hash = get_hash(filename, first_chunk_only=True)
            except (OSError,):
                continue

            duplicate = hashes_on_1k.get(small_hash)
            if duplicate:
                hashes_on_1k[small_hash].append(filename)
            else:
                # create the list for this 1k hash
                hashes_on_1k[small_hash] = []
                hashes_on_1k[small_hash].append(filename)

    # For all files with the hash on the 1st 1024 bytes,
    # get their hash on the full file - collisions will be duplicates
    for __, files in hashes_on_1k.items():
        if len(files) < 2:
            result.extend(files)
            continue    # this hash of fist 1k file bytes is unique, no need to spend cpu cycles on it

        for filename in files:
            try:
                full_hash = get_hash(filename, first_chunk_only=False)
            except (OSError,):
                # the file access might've changed till the exec point got here
                continue

            duplicate = hashes_full.get(full_hash)
            if not duplicate:
                hashes_full[full_hash] = filename
                result.append(filename)

    return result


def extract_comment_java(file_list):
    """
    Given file_list, extract all Java comments from them and return a dictionary to describe it
    Please note that some
    """
    result = {}
    for path in file_list:
        result[path] = {}
        result[path]['size'] = os.path.getsize(path)
        result[path]['comments'] = []
        src = ''

        try:
            with open(path, 'r') as f:
                src = f.read()
        except UnicodeDecodeError as e:
            # With unknown encoding, only extract the ascii part of code
            # See https://docs.python.org/3/howto/unicode.html
            with open(path, 'r', encoding="ascii", errors="surrogateescape") as f:
                src = f.read()

        # Detect and extract comments
        regex1 = re.compile(r'/\*.*?\*/', flags=re.DOTALL)
        regex2 = re.compile(r'//.*?$', flags=re.MULTILINE)
        for match in re.finditer(regex1, src):
            result[path]['comments'].append({
                'content': match.group(0),
                'span': match.span()
            })
        for match in re.finditer(regex2, src):
            result[path]['comments'].append({
                'content': match.group(0),
                'span': match.span()
            })
    return result


def process_worker(proj_path, index, row):
    """
    This is the task unit for each process to run
    """
    # Skip if the comment data file already exists
    # Delete the temp/comment_data/ folder if you want to rerun the whole process
    if os.path.exists('temp/comment_data/{}.json'.format(row['name'])):
        print(colored('Skipping {} because the data already exists...'.format(
            row['name']), 'yellow'))
        return

    # TODO: Language other than Java is not currently supported
    if row['language'] != 'Java':
        print(colored('Warning: Language not Java, skipping {}...'.format(
            row['name']), 'red'))
        return
    print(colored('{}: Processing {}...'.format(index, row['name']), 'green'))

    # Find all unique source code files from a project
    path = os.path.join(proj_path, row['name'])
    suffix_mapping = {'Java': '.java',
                      'JavaScript': '.js', 'Python': '.py'}
    suffix = suffix_mapping[row['language']]
    file_list = get_file_list(path, suffix)
    read_count = len(file_list)
    file_list = remove_duplicates(file_list)
    print('Read {} source files in which {} files are unique...'.format(
        read_count, len(file_list)))

    # Process comments
    result = extract_comment_java(file_list)
    with open('temp/comment_data/{}.json'.format(row['name']), 'w') as f:
        f.write(json.dumps(result, indent=2))
    return


if __name__ == '__main__':
    # Set the recursion limit from 1000 to 10000 to avoid RecursionError
    # because when parsing AST, the tree might have very high depth
    sys.setrecursionlimit(10000)
    resource.setrlimit(resource.RLIMIT_STACK, (2**28, -1))

    # Parse Arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        'csv_path', help='Path to the CSV file storing project information')
    parser.add_argument('proj_path', help='Path where the projects are stored')
    parser.add_argument('-j', type=int, default=8,
                        help='Number of Jobs (Default 8)')
    args = vars(parser.parse_args())
    csv_path = args['csv_path']
    proj_path = args['proj_path']
    num_jobs = args['j']

    # Initialize output directories
    os.makedirs('temp/comment_data/', exist_ok=True)

    projects = pd.read_csv(csv_path)

    # Extract comments with process pooling
    pool = multiprocessing.Pool(num_jobs)
    for index, row in projects.iterrows():
        # pool.apply_async(process_worker, args=(proj_path, index, row))
        process_worker(proj_path, index, row)
    pool.close()
    pool.join()

    print(colored('Writing results to CSV...', 'green'))

    # Initialize metrics if not present
    # TODO Add more metrics to them
    # metrics = ['src_files', 'header_comment', 'functions', 'func_with_doc', 'doc_comment', 'impl_comment']
    metrics = ['src_files', 'src_file_size',
               'comment_size', 'doc_comment', 'impl_comment']
    for metric in metrics:
        if metric not in projects:
            projects[metric] = -1

    # Calculate metric for each project
    for index, row in projects.iterrows():
        sys.stdout.write('\r')
        sys.stdout.write('{}/{} Projects'.format(index + 1, len(projects['name'])))
        sys.stdout.flush()

        with open('temp/comment_data/{}.json'.format(row['name']), 'r') as f:
            comments = json.load(f)
        projects.at[index, 'src_files'] = len(comments)
        projects.at[index, 'src_file_size'] = sum(
            [val['size'] for val in comments.values()])
        comment_size = 0
        doc_comment = 0
        impl_comment = 0
        for key, val in comments.items():
            for comment in val['comments']:
                comment_size += len(comment['content'])
                if comment['content'].startswith('/**'):
                    doc_comment += 1
                else:
                    impl_comment += 1
        projects.at[index, 'comment_size'] = comment_size
        projects.at[index, 'doc_comment'] = doc_comment
        projects.at[index, 'impl_comment'] = impl_comment
    
    projects.to_csv(csv_path, index=False)
    print(colored('Finished!', 'green'))
