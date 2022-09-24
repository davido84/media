import argparse
import logging
import dup
import music_titles
from music_titles import fix_titles
from dup import rm_dup
import media_util
from music_tag import tag_music_files_from_filename

#  ffmpeg flac:
#  $ ffmpeg -i $SOURCE -q:a flac -compression_level 12 $OUT.flac

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%m-%d %H:%M',
                    filename='d:/cm.log',
                    filemode='w')

# define a Handler which writes INFO messages or higher to the sys.stderr
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)

# set a format which is simpler for console use
# formatter = logging.Formatter('%(name)-12s: %(levelname)-8s %(message)s')
formatter = logging.Formatter('%(levelname)-8s %(message)s')
# tell the handler to use this format
console.setFormatter(formatter)
# add the handler to the root logger
logging.getLogger().addHandler(console)

# Now, define a couple of other loggers which might represent areas in your
# application:

# logger1 = logging.getLogger('myapp.area1')
# logger2 = logging.getLogger('myapp.area2')
#
# logger1.debug('Quick zephyrs blow, vexing daft Jim.')
# logger1.info('How quickly daft jumping zebras vex.')
# logger2.warning('Jail zesty vixen who grabbed pay from quack.')
# logger2.error('The five boxing wizards jump quickly.')


def main():
    parser = argparse.ArgumentParser(description='Convert Music Files')
    parser.add_argument('-i', '--input', type=str, help='Input folder or file', default='e:/music')
    parser.add_argument('-d', '--dry-run', action='store_true', default=False, help='Dry run')
    parser.add_argument('-f', '--force', action='store_true', default=False, help='Force action')

    subparsers = parser.add_subparsers(title='Commands', description='Convert music commands')

    parser_fix_titles = subparsers.add_parser('fix-titles', description='Fix music filenames', aliases=['ft'])
    parser_fix_titles.add_argument('-v', '--validate', action='store_true',
                                   help='Validate all titles are in canonical format.')
    parser_fix_titles.set_defaults(func=fix_titles,
                                   desc=parser_fix_titles.description)

    parser_remove_duplicates = subparsers.add_parser('rm-dup', description='Remove duplicates', aliases=['rd'])
    parser_remove_duplicates.set_defaults(func=rm_dup,
                                          desc=parser_remove_duplicates.description)

    parser_remove_duplicates = subparsers.add_parser('dup', description='Show duplicate files.')
    parser_remove_duplicates.set_defaults(func=dup.show_duplicates,
                                          desc=parser_remove_duplicates.description)

    parser_validate_metadata = subparsers.add_parser('validate-meta',
                                                     description='Validate metadata.', aliases=['vm'])
    parser_validate_metadata.set_defaults(func=music_titles.validate_metadata,
                                          desc=parser_validate_metadata.description)

    parser_tag_files = subparsers.add_parser('tag', description='Tag music files from filenames')
    parser_tag_files.add_argument('--delete', help='Delete tags', action='store_true', default=False)
    parser_tag_files.set_defaults(func=tag_music_files_from_filename, desc=parser_tag_files.description)

    args = parser.parse_args()
    with media_util.media_method(args.desc):
        args.func(args)


if __name__ == "__main__":
    main()