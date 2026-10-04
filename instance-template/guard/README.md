Plain-text lists, one entry per line, `#` for comments:

- private-names.txt      words that must never reach the generic repository (site codes, family or room
                         names, hostnames); 4+ characters, any case. The sanitizer turns them into SITE.
- sanitizer-words.txt    short private words (under 4 characters), matched as whole words with exact case.
- allow.txt              values the commit guard should let through (for example a public product model
                         that your hostname happens to equal).
- trim-exclude.txt       words that must not appear in the public sample set (for example service names).
