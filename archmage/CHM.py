# -*- coding: utf-8 -*-
#
# archmage -- CHM decompressor
# Copyright (c) 2003 Eugeny Korekin <aaaz@users.sourceforge.net>
# Copyright (c) 2005-2009 Basil Shubin <bashu@users.sourceforge.net>
# Copyright (c) 2015-2020 Misha Gusarov <dottedmag@dottedmag.net>
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA 02110-1301, USA.
#

import os
import sys
import re
import shutil
import errno
import string
import tempfile
import os.path
from enum import Enum
from typing import List, Union

import archmage

from archmage.CHMParser import SitemapFile, PageLister, ImageCatcher, TOCCounter

# import PyCHM bindings
try:
    from chm import chmlib  # type: ignore
except ImportError as msg:
    sys.exit(
        "ImportError: %s\nPlease check README file for system requirements."
        % msg
    )

# External file converters
from archmage.chmtotext import chmtotext
from archmage.htmldoc import htmldoc


class Action(Enum):
    """
    Defines five enums representing different actions for handling CHM files:
    `EXTRACT`, `DUMPHTML`, `CHM2TXT`, `CHM2HTML`, and `CHM2PDF`.

    Attributes:
        EXTRACT (Enum): 1.
        DUMPHTML (int): 2 in value, indicating that it performs the action of
            dumping HTML content to a file or directory.
        CHM2TXT (3digit): 3-dimensional, meaning it can be any combination of three
            values: 1, 2, or 3.
        CHM2HTML (int): 4th in the enumeration, representing the action of converting
            CHM files to HTML format.
        CHM2PDF (Integer): 5, indicating that it is a method for converting Chemical
            Markup Language (ChemML) files to Portable Document Format (PDF) files.

    """
    EXTRACT = 1
    DUMPHTML = 2
    CHM2TXT = 3
    CHM2HTML = 4
    CHM2PDF = 5


PARENT_RE = re.compile(r"(^|/|\\)\.\.(/|\\|$)")


class FileSource:
    """
    Provides methods for listing and retrieving files from a CHM (Help File) file,
    as well as a `close()` method to release resources when no longer needed.

    Attributes:
        _chm (chmlibChmFile): Used to manage a CHM file.

    """
    def __init__(self, filename):
        self._chm = chmlib.chm_open(filename)

    def listdir(self):
        """
        Within the `FileSource` class enumerates files and directories within a
        Chemical Document Management (CDM) file using the `chm_enumerate` function
        from the CHMLIB library. It appends the path of each file or directory to
        a list (`out`) and returns the list.

        Returns:
            Liststr: An enumerated list of file paths from a given Chemistry Machine
            (CHM) file.

        """
        def get_name(chmfile, ui, out):
            """
            Takes a `chmfile`, `ui`, and `out` parameters, and appends the path
            to a list if it is not the root directory `/`.

            Args:
                chmfile (object): Used to represent a file path.
                ui (chmfilePath): Used to store the path of the UI file.
                out (stdvectorstdstring): Used to store the paths of the files
                    found in the given directory.

            Returns:
                chmlibCHM_ENUMERATOR_CONTINUE: A

            """
            path = ui.path.decode("utf-8")
            if path != "/":
                out.append(path)
            return chmlib.CHM_ENUMERATOR_CONTINUE

        out: List[str] = []
        if (
            chmlib.chm_enumerate(
                self._chm, chmlib.CHM_ENUMERATE_ALL, get_name, out
            )
            == 0
        ):
            sys.exit("UnknownError: CHMLIB or PyCHM bug?")
        return out

    def get(self, name):
        """
        Resolves an object in the CHM file using the `chmlib.chm_resolve_object()`
        method, retrieves the contents of the resolved object using the
        `chmlib.chm_retrieve_object()` method, and returns the retrieved content.

        Args:
            name (str): Used to resolve an object in the CHM library.

        Returns:
            object: A resolved and retrieved object from the CHM library.

        """
        result, ui = chmlib.chm_resolve_object(self._chm, name.encode("utf-8"))
        if result != chmlib.CHM_RESOLVE_SUCCESS:
            return None
        size, content = chmlib.chm_retrieve_object(self._chm, ui, 0, ui.length)
        if size == 0:
            return None
        return content

    def close(self):
        chmlib.chm_close(self._chm)


class DirSource:
    """
    Provides methods for listing directory contents and reading file contents,
    along with a `close()` method for cleaning up resources.

    Attributes:
        dirname (str): Initialized in the constructor with the value passed as
            argument to the constructor. It represents
            the root directory of the source files to be listed or read from.

    """
    def __init__(self, dirname):
        self.dirname = dirname
    def listdir(self):
        """
        Recursively traverses subdirectories of a specified directory and returns
        a list of relative file paths within those subdirectories.

        Returns:
            list: A collection of strings representing the file paths relative to
            the directory path provided as an argument.

        """
        entries = []
        for dir, _, files in os.walk(self.dirname):
            for f in files:
                entries.append(
                    "/" + os.path.relpath(os.path.join(dir, f), self.dirname)
                )
        return entries
    def get(self, filename):
        """
        Within the `DirSource` class reads a file from a specified directory and
        returns its contents as a binary read stream.

        Args:
            filename (str): Used to specify the file name to read from.

        Returns:
            object: The result of reading a file from a specified directory and
            file name.

        """
        with open(self.dirname + filename, "rb") as fh:
            if fh is None:
                return None
            return fh.read()
    def close(self):
        pass


class CHM:
    """
    Manages the extraction and processing of CHM files, including creating a
    temporary directory for storing extracted content, and executing various
    commands to create HTML or PDF outputs. It also provides methods for extracting
    individual entries from the CHM file and manipulating images within the file.

    Attributes:
        cache (instance): Used to keep track of the files that have been extracted
            from a CHM file. It stores a set of tuples, each containing the file
            name and its corresponding path.
        source (instance): Used to represent the source file or directory from
            which the CHM content will be extracted.
        sourcename (str): Used to store the source name or path from where the CHM
            files are extracted.
        __dict__ (instance): A dictionary containing all the attributes and methods
            of the class, which can be accessed by their name.
        aux_re (regular): Used to match names that are not valid CHM entries but
            may still be interesting or problematic, such as parent directories
            or malicious filenames.
        auxes (list): Used to store regular expressions for matching entries that
            should be skipped
            during extraction. The regular expressions are used to identify malicious
            or
            undesirable entries in the CHM file.
        topicstree (instance): A list of tuples, where each tuple contains a topic
            name and a list of subtopics related to that topic. It is used to store
            the hierarchical structure of the CHM document's topics.
        topics (instance): A list of tuples, where each tuple contains a topic
            name and a list of file paths that belong to that topic. It is used
            to keep track of the files related to each topic in the CHM document.
        contents (str): Used to store a list of file paths that are extracted from
            the CHM file using the `extract_entries()` method.

    """
    def __init__(self, name):
        """
        Initializes an instance of `CHM` by setting member variables, compiling
        code from a configuration file, and creating a `SitemapFile` object to
        store the topic tree.

        Args:
            name (str): Used to set the name of the configuration file or directory
                being initialized.

        """
        self.cache = {}
        # Name of source directory with CHM content
        if os.path.isdir(name):
            self.source: Union[DirSource, FileSource] = DirSource(name)
        else:
            self.source = FileSource(name)
        self.sourcename = name
        # Import variables from config file into namespace
        exec(
            compile(
                open(archmage.config, "rb").read(), archmage.config, "exec"
            ),
            self.__dict__,
        )

        # build regexp from the list of auxiliary files
        self.aux_re = "|".join([re.escape(s) for s in self.auxes])

        # Get and parse 'Table of Contents'
        try:
            self.topicstree = self.topics()
        except AttributeError:
            self.topicstree = None
        self.contents = SitemapFile(self.topicstree).parse()

    def close(self):
        self.source.close()

    def entries(self):
        """
        Retrieves and caches the list of entries associated with an instance of
        the `CHM` class, or computes and cache them if they are not already present
        in the cache.

        Returns:
            list: A cache of entries.

        """
        if "entries" not in self.cache:
            self.cache["entries"] = self._entries()
        return self.cache["entries"]

    def _entries(self):
        return self.source.listdir()

    # retrieves the list of HTML files contained into the CHM file, **in order**
    # (that's the important bit).
    # (actually performed by the PageLister class)
    def html_files(self):
        """
        Retrieves and stores a list of HTML files associated with an object of
        class `CHM` in its cache for future use.

        Returns:
            list: A cache of the html files in the system.

        """
        if "html_files" not in self.cache:
            self.cache["html_files"] = self._html_files()
        return self.cache["html_files"]

    def _html_files(self):
        """
        Within the CHM class takes the `PageLister` object as input, feeds it with
        the `topicstree`, and returns the resulting pages list.

        """
        lister = PageLister()
        lister.feed(self.topicstree)
        return lister.pages

    # retrieves the list of images urls contained into the CHM file.
    # (actually performed by the ImageCatcher class)
    def image_urls(self):
        """
        Retrieves and stores image URLs in a class instance's cache for later use.

        Returns:
            str: A cache of image URLs.

        """
        if "image_urls" not in self.cache:
            self.cache["image_urls"] = self._image_urls()
        return self.cache["image_urls"]

    def _image_urls(self):
        """
        Within the CHM class fetches images from HTML files and adds them to a
        list of image URLs.

        """
        out: List[str] = []
        image_catcher = ImageCatcher()
        for file in self.html_files():
            # Use latin-1, as it will accept any byte sequences
            image_catcher.feed(
                Entry(
                    self.source, file, self.filename_case, self.restore_framing
                ).correct().decode("latin-1")
            )
            for image_url in image_catcher.imgurls:
                if not out.count(image_url):
                    out.append(image_url)
        return out

    # retrieves a dictionary of actual file entries and corresponding urls into
    # the CHM file
    def image_files(self):
        """
        In the `CHM` class retrieves and caches a list of image files for the
        current application.

        Returns:
            list: A cache of image files.

        """
        if "image_files" not in self.cache:
            self.cache["image_files"] = self._image_files()
        return self.cache["image_files"]

    def _image_files(self):
        """
        In the CHM class takes an input of self.image_urls() and then iterates
        through each entry in the list. If a match is found between the entry and
        image_url, it updates the output dictionary with the entry and its
        corresponding image URL. Finally, it returns the updated output dictionary.

        """
        out = {}
        for image_url in self.image_urls():
            for entry in self.entries():
                if (
                    re.search(image_url, entry.lower())
                    and entry.lower() not in out
                ):
                    out.update({entry: image_url})
        return out

    # Get topics file
    def topics(self):
        """
        Retrieves and caches a list of topics associated with an instance of the
        `CHM` class, if not already cached.

        Returns:
            list: A cache of the topics as obtained from calling the private method
            `_topics`.

        """
        if "topics" not in self.cache:
            self.cache["topics"] = self._topics()
        return self.cache["topics"]

    def _topics(self):
        """
        Within a `CHM` class takes an `Entry` object as input and returns a new
        `Entry` object with additional properties based on the original input.

        """
        for e in self.entries():
            if e.lower().endswith(".hhc"):
                return Entry(
                    self.source,
                    e,
                    self.filename_case,
                    self.restore_framing,
                    frontpage=self.frontpage(),
                ).get()

    # use first page as deftopic. Note: without heading slash
    def deftopic(self):
        """
        In the `CHM` class determines whether a given topic has already been defined
        in the cache and returns the cached value if it exists, or calls the
        `_deftopic` method to create a new definition otherwise.

        Returns:
            object: Stored in a cache for future use.

        """
        if "deftopic" not in self.cache:
            self.cache["deftopic"] = self._deftopic()
        return self.cache["deftopic"]

    def _deftopic(self):
        """
        In the CHM class checks if the input HTML file starts with a slash, and
        if so, removes it and returns the remaining part in lowercase. If not, it
        simply returns the HTML file in lowercase.

        """
        if self.html_files()[0].startswith("/"):
            return self.html_files()[0].replace("/", "", 1).lower()
        return self.html_files()[0].lower()

    # Get frontpage name
    def frontpage(self):
        """
        Determines if it has been called before, and if not, it calls its internal
        method `_frontpage` to generate the front page content, then stores it in
        the cache for future use.

        Returns:
            object: Cache entry containing a page to display as front page.

        """
        if "frontpage" not in self.cache:
            self.cache["frontpage"] = self._frontpage()
        return self.cache["frontpage"]

    def _frontpage(self):
        """
        Determines the front page of a web application by checking if a given file
        is the index file, and if so, creates a new index file with a numbered name.

        """
        frontpage = os.path.join("/", "index.html")
        index = 2  # index2.html and etc.
        for filename in self.entries():
            if frontpage == filename:
                frontpage = os.path.join("/", ("index%s.html" % index))
                index += 1
        return frontpage

    # Get all templates files
    def templates(self):
        """
        Retrieves a list of templates belonging to an instance of the `CHM` class
        from its cache if it's not already stored, then returns the stored list.

        Returns:
            dict: A cache of templates.

        """
        if "templates" not in self.cache:
            self.cache["templates"] = self._templates()
        return self.cache["templates"]

    def _templates(self):
        """
        In the CHM class lists all files in a directory and filters them based on
        whether they are already included in a list of entries, returning the
        remaining files.

        """
        out = []
        for file in os.listdir(self.templates_dir):
            if os.path.isfile(os.path.join(self.templates_dir, file)):
                if os.path.join("/", file) not in self.entries():
                    out.append(os.path.join("/", file))
        return out

    # Get ToC levels
    def toclevels(self):
        """
        In the `CHM` class returns a cached value or computes it from the `_toclevels`
        method if it's not already in the cache.

        Returns:
            dict: A cache of toclevels for the given instance of the class.

        """
        if "toclevels" not in self.cache:
            self.cache["toclevels"] = self._toclevels()
        return self.cache["toclevels"]

    def _toclevels(self):
        """
        Counts the number of topics at each level in a given hierarchy and returns
        the maximum level reached or the total number of topics if it exceeds the
        maximum allowed level.

        """
        counter = TOCCounter()
        # Use latin-1, as it will accept any byte sequences
        counter.feed(self.topicstree.decode("latin-1"))
        if counter.count > self.maxtoclvl:
            return self.maxtoclvl
        else:
            return counter.count

    def get_template(self, name):
        """
        In the `CHM` class retrieves an HTML template based on the input `name`
        and replaces placeholders with class variables values, returning the
        substituted template as a string.

        Args:
            name (str): Used to specify the name of the template to be loaded from
                the templates directory.

        Returns:
            stringTemplate: A instance of class `string.Template` that has been
            filled with placeholders substituted with values from a dictionary.

        """
        if name == self.frontpage():
            tpl = open(os.path.join(self.templates_dir, "index.html")).read()
        else:
            tpl = open(
                os.path.join(self.templates_dir, os.path.basename(name))
            ).read()
        params = {
            "title": self.title,
            "contents": self.contents,
            "deftopic": self.deftopic(),
            "bcolor": self.bcolor,
            "fcolor": self.fcolor,
        }
        return string.Template(tpl).substitute(params)

    def process_templates(self, destdir="."):
        """
        Writes HTML files and icons to a specified directory based on templates
        and frontpage.

        Args:
            destdir (str): Used to specify the directory where the templates are
                written. It has an initial value of ".", which means the current
                working directory.

        """
        for template in self.templates():
            open(os.path.join(destdir, os.path.basename(template)), "w").write(
                self.get_template(template)
            )
        if self.frontpage() not in self.templates():
            open(
                os.path.join(destdir, os.path.basename(self.frontpage())), "w"
            ).write(self.get_template("index.html"))
        if not os.path.exists(os.path.join(destdir, "icons/")):
            shutil.copytree(
                os.path.join(self.icons_dir), os.path.join(destdir, "icons/")
            )

    def extract_entry(self, entry, output_file, destdir=".", correct=False):
        # process output entry, remove first '/' in entry name
        """
        Takes an entry object, an output file path, and a flag for correctness or
        not, and creates or overwrites the corresponding entry file in the specified
        directory with the correct or incorrect content respectively.

        Args:
            entry (Entry): Passed as an argument to the function. It represents
                an entry in a file or directory that needs to be extracted.
            output_file (str): Used to specify the output file for writing extracted
                entries.
            destdir (str): Used to specify the directory where the output file
                will be saved. It can be either an absolute path or a relative
                path from the current working directory.
            correct (bool): Used to indicate whether the entry should be corrected
                or not during extraction.

        """
        fname = output_file.lower().replace("/", "", 1)
        # get directory name for file fname if any
        dname = os.path.dirname(os.path.join(destdir, fname))
        # if dname is a directory and it's not exist, than create it
        if dname and not os.path.exists(dname):
            os.makedirs(dname)
        # otherwise write a file from CHM entry
        if not os.path.isdir(os.path.join(destdir, fname)):
            # write CHM entry content into the file, corrected or as is
            if correct:
                open(os.path.join(destdir, fname), "wb").write(
                    Entry(
                        self.source,
                        entry,
                        self.filename_case,
                        self.restore_framing,
                    ).correct()
                )
            else:
                open(os.path.join(destdir, fname), "wb").write(
                    Entry(
                        self.source,
                        entry,
                        self.filename_case,
                        self.restore_framing,
                    ).get()
                )

    def extract_entries(self, entries=[], destdir=".", correct=False):
        """
        In the `CHM` class takes an optional list of entries, searches for malicious
        names, and extracts entries matching a regular expression pattern or a
        parent directory.

        Args:
            entries (listarray): An array of strings representing files or paths
                to files that need to be extracted.
            destdir (str): Used to specify the directory where the extracted entries
                will be saved. It has a default value of ".".
            correct (bool): Used to indicate whether the entry should be extracted
                with its original path or as a relative path within the destination
                directory.

        """
        for e in entries:
            # if entry is auxiliary file, than skip it
            if re.match(self.aux_re, e):
                continue
            if PARENT_RE.search(e):
                raise RuntimeError("Giving up on malicious name: %s" % e)
            self.extract_entry(
                e, output_file=e, destdir=destdir, correct=correct
            )

    def extract(self, destdir):
        """
        Performs an action on a destination directory, creating it if it does not
        exist and executing actions related to its entries and templates.

        Args:
            destdir (str): Used to specify the destination directory for the
                extraction of the entries from the jar file.

        """
        try:
            # Create destination directory
            os.mkdir(destdir)
            # make raw content extraction
            self.extract_entries(entries=self.entries(), destdir=destdir)
            # process templates
            self.process_templates(destdir=destdir)
        except OSError as error:
            if error.errno == errno.EEXIST:
                sys.exit("%s is already exists" % destdir)

    def dump_html(self, output=sys.stdout):
        """
        Within the `CHM` class iterates over a list of HTML files and prints the
        contents of each file to the output stream if it does not match a specific
        regular expression pattern.

        Args:
            output (object): Used to represent the destination for printing the
                output of the function.

        """
        for e in self.html_files():
            # if entry is auxiliary file, than skip it
            if re.match(self.aux_re, e):
                continue
            print(
                Entry(
                    self.source, e, self.filename_case, self.restore_framing
                ).get(),
                file=output,
            )

    def chm2text(self, output=sys.stdout):
        """
        Processes CHM files by calling the `chmtotext` command with input from an
        Entry object, and outputting the result to the specified destination.

        Args:
            output (object): Used to specify the destination for the generated text.

        """
        for e in self.html_files():
            # if entry is auxiliary file, than skip it
            if re.match(self.aux_re, e):
                continue
            # to use this function you should have 'lynx' or 'elinks' installed
            chmtotext(
                input=Entry(
                    self.source, e, self.filename_case, self.restore_framing
                ).get(),
                cmd=self.chmtotext,
                output=output,
            )

    def htmldoc(self, output, format=Action.CHM2HTML):
        """
        Extracts CHM content into a temporary directory, processes images, and
        generates HTML or PDF output using the specified format and options.

        Args:
            output (str): Used to specify the output file path for the generated
                HTML document, which can be a directory or a file name.
            format (str): Used to specify the output format of the documentation,
                which can be either CHM2HTML or CHM2PDF.

        """
        # Extract CHM content into temporary directory
        output = output.replace(" ", "_")
        tempdir = tempfile.mkdtemp(prefix=output.rsplit(".", 1)[0])
        self.extract_entries(
            entries=self.html_files(), destdir=tempdir, correct=True
        )
        # List of temporary files
        files = [
            os.path.abspath(tempdir + file.lower())
            for file in self.html_files()
        ]
        if format == Action.CHM2HTML:
            options = self.chmtohtml
            # change output from single html file to a directory with html file
            # and images
            if self.image_files():
                dirname = archmage.file2dir(output)
                if os.path.exists(dirname):
                    sys.exit("%s is already exists" % dirname)
                # Extract image files
                os.mkdir(dirname)
                # Extract all images
                for key, value in list(self.image_files().items()):
                    self.extract_entry(
                        entry=key, output_file=value, destdir=dirname
                    )
                # Fix output file name
                output = os.path.join(dirname, output)
        elif format == Action.CHM2PDF:
            options = self.chmtopdf
            if self.image_files():
                # Extract all images
                for key, value in list(self.image_files().items()):
                    self.extract_entry(
                        entry=key, output_file=key.lower(), destdir=tempdir
                    )
        htmldoc(files, self.htmldoc_exec, options, self.toclevels(), output)
        # Remove temporary files
        shutil.rmtree(path=tempdir)


class Entry(object):
    """
    Manages entry content, including reading and manipulating links, adding restore
    framing JavaScript, and returning the corrected entry content.

    Attributes:
        source (object): Used to store a string containing the entry's content.
        name (str): Used to store the name of the entry.
        filename_case (str): Used to modify the file name in lower case when
            searching for links.
        restore_framing (bool): Used to specify whether the framing links should
            be restored or not.
        frontpage (ospathbasename): Used to specify the name of the file containing
            the front page content for the entry.

    """

    def __init__(
        self,
        source,
        name,
        filename_case,
        restore_framing,
        frontpage="index.html",
    ):
        # Entry source
        """
        Of the `Entry` class initializes attributes with user-provided values:
        source, name, filename_case, restore_framing, and frontpage.

        Args:
            source (object): Used to store the source code for a web page or document.
            name (str): Assigned the value passed as argument during initialization,
                representing the name of the page or resource being initialized.
            filename_case (str): Used to specify the case of the file name, which
                can be "lower", "upper", or "title".
            restore_framing (int): 1 by default, indicating that the frame should
                be restored when loading the HTML file.
            frontpage (ospathbasename): Set to the value of "index.html" by default,
                indicating that the initial front page of the web application
                should be the file named "index.html".

        """
        self.source = source
        # object inside CHM file
        self.name = name
        self.filename_case = filename_case
        self.restore_framing = restore_framing
        # frontpage name to substitute
        self.frontpage = os.path.basename(frontpage)

    def read(self):
        return self.source.get(self.name)

    def lower_links(self, text):
        """
        Replaces certain words in a given string with their lowercase versions,
        specifically the keywords `href` and `src`.

        Args:
            text (str): A string of text to be processed for lowercase links.

        Returns:
            str: The result of applying a regular expression to the input string
            using a lambda function.

        """
        return re.sub(
            b"(?i)(href|src)\\s*=\\s*([^\\s|>]+)",
            lambda m: m.group(0).lower(),
            text,
        )

    def add_restoreframing_js(self, name, text):
        """
        Modifies JavaScript code based on the `name` parameter, replacing certain
        characters and generating a new script tag with depth-dependent modifications.

        Args:
            name (str): Used to generate a script tag for restoring framing pages.
            text (HTML): Modified by replacing any occurrence of `<body>` with the
                encoded JavaScript code.

        Returns:
            str: A modified version of the input string `text`, with certain
            elements replaced or removed using regular expressions and string concatenation.

        """
        name = re.sub("/+", "/", name)
        depth = name.count("/")

        js = b"""<body><script language="javascript">
if (window.name != "content")
    document.write("<center><a href='%s%s?page=%s'>show framing</a></center>")
</script>""" % (
            b"../" * depth,
            self.frontpage.encode("utf8"),
            name.encode("utf8"),
        )

        return re.sub(b"(?i)<\\s*body\\s*>", js, text)

    def correct(self):
        """
        Reads data from an entry, modifies it by removing certain tags and strings,
        and returns the modified data.

        Returns:
            str: A modified version of the input data, with certain elements removed
            or replaced.

        """
        data = self.read()
        # If entry is a html page?
        if re.search("(?i)\\.html?$", self.name) and data is not None:
            # lower-casing links if needed
            if self.filename_case:
                data = self.lower_links(data)

            # Delete unwanted HTML elements.
            data = re.sub(b"<div .*teamlib\\.gif.*\\/div>", b"", data)
            data = re.sub(b"<a href.*>\\[ Team LiB \\]<\\/a>", b"", data)
            data = re.sub(
                b"<table.*larrow\\.gif.*rarrow\\.gif.*<\\/table>", b"", data
            )
            data = re.sub(b"<a href.*next\\.gif[^>]*><\\/a>", b"", data)
            data = re.sub(b"<a href.*previous\\.gif[^>]*><\\/a>", b"", data)
            data = re.sub(b"<a href.*prev\\.gif[^>]*><\\/a>", b"", data)
            data = re.sub(b'"[^"]*previous\\.gif"', b'""', data)
            data = re.sub(b'"[^"]*prev\\.gif"', b'""', data)
            data = re.sub(b'"[^"]*next\\.gif"', b'""', data)
        if data is not None:
            return data
        else:
            return b""

    def get(self):
        """
        Retrieves entry content by reading its file and performing modifications
        based on the object's properties, such as lowercasing links and restoring
        framing HTML tags if necessary. It returns the modified content or an empty
        string if none exists.

        Returns:
            str: A string containing the entry content.

        """
        # read entry content
        data = self.read()
        # If entry is a html page?
        if re.search("(?i)\\.html?$", self.name) and data is not None:
            # lower-casing links if needed
            if self.filename_case:
                data = self.lower_links(data)
            # restore framing if that option is set in config file
            if self.restore_framing:
                data = self.add_restoreframing_js(self.name[1:], data)
        if data is not None:
            return data
        else:
            return b""
