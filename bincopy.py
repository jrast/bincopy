"""Mangling of various file formats that conveys binary information
(Motorola S-Record, Intel HEX, TI-TXT and binary files).

"""

from __future__ import print_function
from __future__ import division

import binascii
import string
import sys
import argparse
from collections import namedtuple

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

from humanfriendly import format_size


__author__ = 'Erik Moqvist'
__version__ = '16.2.0'


DEFAULT_WORD_SIZE_BITS = 8

# Intel hex types.
IHEX_DATA = 0
IHEX_END_OF_FILE = 1
IHEX_EXTENDED_SEGMENT_ADDRESS = 2
IHEX_START_SEGMENT_ADDRESS = 3
IHEX_EXTENDED_LINEAR_ADDRESS = 4
IHEX_START_LINEAR_ADDRESS = 5

# TI-TXT defines
TI_TXT_BYTES_PER_LINE = 16


class Error(Exception):
    """Bincopy base exception.

    """

    pass


class UnsupportedFileFormatError(Error):
    pass


class AddDataError(Error):
    pass


def crc_srec(hexstr):
    """Calculate the CRC for given Motorola S-Record hexstring.

    """

    crc = sum(bytearray(binascii.unhexlify(hexstr)))
    crc &= 0xff
    crc ^= 0xff

    return crc


def crc_ihex(hexstr):
    """Calculate the CRC for given Intel HEX hexstring.

    """

    crc = sum(bytearray(binascii.unhexlify(hexstr)))
    crc &= 0xff
    crc = ((~crc + 1) & 0xff)

    return crc


def pack_srec(type_, address, size, data):
    """Create a Motorola S-Record record of given data.

    """

    if type_ in '0159':
        line = '{:02X}{:04X}'.format(size + 2 + 1, address)
    elif type_ in '268':
        line = '{:02X}{:06X}'.format(size + 3 + 1, address)
    elif type_ in '37':
        line = '{:02X}{:08X}'.format(size + 4 + 1, address)
    else:
        raise Error(
            "expected record type 0..3 or 5..9, but got '{}'".format(type_))

    if data:
        line += binascii.hexlify(data).decode('ascii').upper()

    return 'S{}{}{:02X}'.format(type_, line, crc_srec(line))


def unpack_srec(record):
    """Unpack given Motorola S-Record record into variables.

    """

    # Minimum STSSCC, where T is type, SS is size and CC is crc.
    if len(record) < 6:
        raise Error("record '{}' too short".format(record))

    if record[0] != 'S':
        raise Error(
            "record '{}' not starting with an 'S'".format(record))

    size = int(record[2:4], 16)
    type_ = record[1:2]

    if type_ in '0159':
        width = 4
    elif type_ in '268':
        width = 6
    elif type_ in '37':
        width = 8
    else:
        raise Error(
            "expected record type 0..3 or 5..9, but got '{}'".format(type_))

    data_offset = (4 + width)
    crc_offset = (4 + 2 * size - 2)

    address = int(record[4:data_offset], 16)
    data = binascii.unhexlify(record[data_offset:crc_offset])
    actual_crc = int(record[crc_offset:], 16)
    expected_crc = crc_srec(record[2:crc_offset])

    if actual_crc != expected_crc:
        raise Error(
            "expected crc '{:02X}' in record {}, but got '{:02X}'".format(
                expected_crc,
                record,
                actual_crc))

    return (type_, address, size - 1 - width // 2, data)


def pack_ihex(type_, address, size, data):
    """Create a Intel HEX record of given data.

    """

    line = '{:02X}{:04X}{:02X}'.format(size, address, type_)

    if data:
        line += binascii.hexlify(data).decode('ascii').upper()

    return ':{}{:02X}'.format(line, crc_ihex(line))


def unpack_ihex(record):
    """Unpack given Intel HEX record into variables.

    """

    # Minimum :SSAAAATTCC, where SS is size, AAAA is address, TT is
    # type and CC is crc.
    if len(record) < 11:
        raise Error("record '{}' too short".format(record))

    if record[0] != ':':
        raise Error("record '{}' not starting with a ':'".format(record))

    size = int(record[1:3], 16)
    address = int(record[3:7], 16)
    type_ = int(record[7:9], 16)

    if size > 0:
        data = binascii.unhexlify(record[9:9 + 2 * size])
    else:
        data = b''

    actual_crc = int(record[9 + 2 * size:], 16)
    expected_crc = crc_ihex(record[1:9 + 2 * size])

    if actual_crc != expected_crc:
        raise Error(
            "expected crc '{:02X}' in record {}, but got '{:02X}'".format(
                expected_crc,
                record,
                actual_crc))

    return (type_, address, size, data)


def is_srec(records):
    try:
        unpack_srec(records.partition('\n')[0].rstrip())
    except Error:
        return False
    else:
        return True


def is_ihex(records):
    try:
        unpack_ihex(records.partition('\n')[0].rstrip())
    except Error:
        return False
    else:
        return True


def is_ti_txt(data):
    try:
        return data[0] in ['@', 'q']
    except IndexError:
        return False


class _Segment(object):
    """A segment is a chunk data with given minimum and maximum address.

    """

    _Chunk = namedtuple('Chunk', ['address', 'data'])

    def __init__(self, minimum_address, maximum_address, data, word_size_bytes):
        self.minimum_address = minimum_address
        self.maximum_address = maximum_address
        self.data = data
        self._word_size_bytes = word_size_bytes

    @property
    def address(self):
        return self.minimum_address // self._word_size_bytes

    def chunks(self, size=32, alignment=1):
        """Return chunks of the data aligned as given by `alignment`. `size`
        must be a multiple of `alignment`. Each chunk is returned as a
        named two-tuple of its address and data.

        """

        if (size % alignment) != 0:
            raise Error(
                'size {} is not a multiple of alignment {}'.format(
                    size,
                    alignment))

        address = self.address
        data = self.data

        # First chunk may be shorter than `size` due to alignment.
        chunk_offset = (address % alignment)

        if chunk_offset != 0:
            first_chunk_size = (alignment - chunk_offset)
            yield self._Chunk(address, data[:first_chunk_size])
            address += (first_chunk_size // self._word_size_bytes)
            data = data[first_chunk_size:]
        else:
            first_chunk_size = 0

        for offset in range(0, len(data), size):
            yield self._Chunk(address + offset // self._word_size_bytes,
                              data[offset:offset + size])

    def add_data(self, minimum_address, maximum_address, data, overwrite):
        """Add given data to this segment. The added data must be adjacent to
        the current segment data, otherwise an exception is thrown.

        """

        if minimum_address == self.maximum_address:
            self.maximum_address = maximum_address
            self.data += data
        elif maximum_address == self.minimum_address:
            self.minimum_address = minimum_address
            self.data = data + self.data
        elif (overwrite
              and minimum_address < self.maximum_address
              and maximum_address > self.minimum_address):
            self_data_offset = minimum_address - self.minimum_address

            # Prepend data.
            if self_data_offset < 0:
                self_data_offset *= -1
                self.data = data[:self_data_offset] + self.data
                del data[:self_data_offset]
                self.minimum_address = minimum_address

            # Overwrite overlapping part.
            self_data_left = len(self.data) - self_data_offset

            if len(data) <= self_data_left:
                self.data[self_data_offset:self_data_offset + len(data)] = data
                data = bytearray()
            else:
                self.data[self_data_offset:] = data[:self_data_left]
                data = data[self_data_left:]

            # Append data.
            if len(data) > 0:
                self.data += data
                self.maximum_address = maximum_address
        else:
            raise AddDataError(
                'data added to a segment must be adjacent to or overlapping '
                'with the original segment data')

    def remove_data(self, minimum_address, maximum_address):
        """Remove given data range from this segment. Returns the second
        segment if the removed data splits this segment in two.

        """

        if ((minimum_address >= self.maximum_address)
            and (maximum_address <= self.minimum_address)):
            raise Error('cannot remove data that is not part of the segment')

        if minimum_address < self.minimum_address:
            minimum_address = self.minimum_address

        if maximum_address > self.maximum_address:
            maximum_address = self.maximum_address

        remove_size = maximum_address - minimum_address
        part1_size = minimum_address - self.minimum_address
        part1_data = self.data[0:part1_size]
        part2_data = self.data[part1_size + remove_size:]

        if len(part1_data) and len(part2_data):
            # Update this segment and return the second segment.
            self.maximum_address = self.minimum_address + part1_size
            self.data = part1_data

            return _Segment(maximum_address,
                            maximum_address + len(part2_data),
                            part2_data,
                            self._word_size_bytes)
        else:
            # Update this segment.
            if len(part1_data) > 0:
                self.maximum_address = minimum_address
                self.data = part1_data
            elif len(part2_data) > 0:
                self.minimum_address = maximum_address
                self.data = part2_data
            else:
                self.maximum_address = self.minimum_address
                self.data = bytearray()

    def __eq__(self, other):
        if isinstance(other, tuple):
            return self.address, self.data == other
        elif isinstance(other, _Segment):
            return ((self.minimum_address == other.minimum_address)
                    and (self.maximum_address == other.maximum_address)
                    and (self.data == other.data)
                    and (self._word_size_bytes == other._word_size_bytes))
        else:
            return False

    def __iter__(self):
        # Allows unpacking as ``address, data = segment``.
        yield self.address
        yield self.data

    def __repr__(self):
        return 'Segment(address={}, data={})'.format(self.address,
                                                     self.data)


class _Segments(object):
    """A list of segments.

    """

    def __init__(self, word_size_bytes):
        self._word_size_bytes = word_size_bytes
        self._current_segment = None
        self._current_segment_index = None
        self._list = []

    def __str__(self):
        return '\n'.join([str(s) for s in self._list])

    def __iter__(self):
        """Iterate over all segments.

        """

        for segment in self._list:
            yield segment

    def __getitem__(self, index):
        try:
            return self._list[index]
        except IndexError:
            raise Error('segment does not exist')

    @property
    def minimum_address(self):
        """The minimum address of the data, or ``None`` if no data is
        available.

        """

        if not self._list:
            return None

        return self._list[0].minimum_address

    @property
    def maximum_address(self):
        """The maximum address of the data, or ``None`` if no data is
        available.

        """

        if not self._list:
            return None

        return self._list[-1].maximum_address

    def add(self, segment, overwrite=False):
        """Add segments by ascending address.

        """

        if self._list:
            if segment.minimum_address == self._current_segment.maximum_address:
                # Fast insertion for adjacent segments.
                self._current_segment.add_data(segment.minimum_address,
                                              segment.maximum_address,
                                              segment.data,
                                              overwrite)
            else:
                # Linear insert.
                for i, s in enumerate(self._list):
                    if segment.minimum_address <= s.maximum_address:
                        break

                if segment.minimum_address > s.maximum_address:
                    # Non-overlapping, non-adjacent after.
                    self._list.append(segment)
                elif segment.maximum_address < s.minimum_address:
                    # Non-overlapping, non-adjacent before.
                    self._list.insert(i, segment)
                else:
                    # Adjacent or overlapping.
                    s.add_data(segment.minimum_address,
                               segment.maximum_address,
                               segment.data,
                               overwrite)
                    segment = s

                self._current_segment = segment
                self._current_segment_index = i

            # Remove overwritten and merge adjacent segments.
            while self._current_segment is not self._list[-1]:
                s = self._list[self._current_segment_index + 1]

                if self._current_segment.maximum_address >= s.maximum_address:
                    # The whole segment is overwritten.
                    del self._list[self._current_segment_index + 1]
                elif self._current_segment.maximum_address >= s.minimum_address:
                    # Adjacent or beginning of the segment overwritten.
                    self._current_segment.add_data(
                        self._current_segment.maximum_address,
                        s.maximum_address,
                        s.data[self._current_segment.maximum_address - s.minimum_address:],
                        overwrite=False)
                    del self._list[self._current_segment_index+1]
                    break
                else:
                    # Segments are not overlapping, nor adjacent.
                    break
        else:
            self._list.append(segment)
            self._current_segment = segment
            self._current_segment_index = 0

    def remove(self, minimum_address, maximum_address):
        new_list = []

        for segment in self._list:
            if (segment.maximum_address <= minimum_address
                or maximum_address < segment.minimum_address):
                # No overlap.
                new_list.append(segment)
            else:
                # Overlapping, remove overwritten parts segments.
                split = segment.remove_data(minimum_address, maximum_address)

                if segment.minimum_address < segment.maximum_address:
                    new_list.append(segment)

                if split:
                    new_list.append(split)

        self._list = new_list

    def chunks(self, size=32, alignment=1):
        """Iterate over all segments and return chunks of the data aligned as
        given by `alignment`. `size` must be a multiple of
        `alignment`. Each chunk is returned as a named two-tuple of
        its address and data.

        """

        if (size % alignment) != 0:
            raise Error(
                'size {} is not a multiple of alignment {}'.format(
                    size,
                    alignment))

        for segment in self:
            for chunk in segment.chunks(size, alignment):
                yield chunk

    def __len__(self):
        """Get the number of segments.

        """

        return len(self._list)


class BinFile(object):
    """A binary file.

    `filenames` may be a single file or a list of files. Each file is
    opened and its data added, given that the format is Motorola
    S-Records, Intel HEX or TI-TXT.

    Set `overwrite` to ``True`` to allow already added data to be
    overwritten.

    `word_size_bits` is the number of bits per word.

    `header_encoding` is the encoding used to encode and decode the
    file header (if any). Give as ``None`` to disable encoding,
    leaving the header as an untouched bytes object.

    """

    def __init__(self,
                 filenames=None,
                 overwrite=False,
                 word_size_bits=DEFAULT_WORD_SIZE_BITS,
                 header_encoding='utf-8'):
        if (word_size_bits % 8) != 0:
            raise Error(
                'word size must be a multiple of 8 bits, but got {} bits'.format(
                    word_size_bits))

        self.word_size_bits = word_size_bits
        self.word_size_bytes = (word_size_bits // 8)
        self._header_encoding = header_encoding
        self._header = None
        self._execution_start_address = None
        self._segments = _Segments(self.word_size_bytes)

        if filenames is not None:
            if isinstance(filenames, str):
                filenames = [filenames]

            for filename in filenames:
                self.add_file(filename, overwrite=overwrite)

    def __setitem__(self, key, data):
        """Write data to given absolute address or address range.

        """

        if isinstance(key, slice):
            if key.start is None:
                address = self.minimum_address
            else:
                address = key.start
        else:
            address = key
            data = hex((0x80 << (8 * self.word_size_bytes)) | data)
            data = binascii.unhexlify(data[4:])

        self.add_binary(data, address, overwrite=True)

    def __getitem__(self, key):
        """Read data from given absolute address or address range.

        """

        if isinstance(key, slice):
            if key.start is None:
                minimum_address = self.minimum_address
            else:
                minimum_address = key.start

            if key.stop is None:
                maximum_address = self.maximum_address
            else:
                maximum_address = key.stop

            return self.as_binary(minimum_address, maximum_address)
        else:
            if key < self.minimum_address or key >= self.maximum_address:
                raise IndexError(
                    'binary file index {} out of range'.format(key))

            return int(binascii.hexlify(self.as_binary(key, key + 1)), 16)


    def __len__(self):
        """Number of words in the file.

        """

        length = sum([len(segment.data) for segment in self.segments])
        length //= self.word_size_bytes

        return length

    def __iadd__(self, other):
        self.add_srec(other.as_srec())

        return self

    def __str__(self):
        return str(self._segments)

    @property
    def execution_start_address(self):
        """The execution start address, or ``None`` if missing.

        """

        return self._execution_start_address

    @execution_start_address.setter
    def execution_start_address(self, address):
        self._execution_start_address = address

    @property
    def minimum_address(self):
        """The minimum address of the data, or ``None`` if the file is empty.

        """

        minimum_address = self._segments.minimum_address

        if minimum_address is not None:
            minimum_address //= self.word_size_bytes

        return minimum_address

    @property
    def maximum_address(self):
        """The maximum address of the data, or ``None`` if the file is empty.

        """

        maximum_address = self._segments.maximum_address

        if maximum_address is not None:
            maximum_address //= self.word_size_bytes

        return maximum_address

    @property
    def header(self):
        """The binary file header, or ``None`` if missing. See
        :class:`BinFile's<.BinFile>` `header_encoding` argument for
        encoding options.

        """

        if self._header_encoding is None:
            return self._header
        else:
            return self._header.decode(self._header_encoding)

    @header.setter
    def header(self, header):
        if self._header_encoding is None:
            if not isinstance(header, bytes):
                raise TypeError(
                    'expected a bytes object, but got {}'.format(type(header)))

            self._header = header
        else:
            self._header = header.encode(self._header_encoding)

    @property
    def segments(self):
        """The segments object. Can be used to iterate over all segments in
        the binary.

        Below is an example iterating over all segments, two in this
        case, and printing them.

        >>> for segment in binfile.segments:
        ...     print(segment)
        ...
        Segment(address=0, data=bytearray(b'\\x00\\x01\\x02'))
        Segment(address=10, data=bytearray(b'\\x03\\x04\\x05'))

        All segments can be split into smaller pieces using the
        `chunks(size=32, alignment=1)` method.

        >>> for chunk in binfile.segments.chunks(2):
        ...     print(chunk)
        ...
        Chunk(address=0, data=bytearray(b'\\x00\\x01'))
        Chunk(address=2, data=bytearray(b'\\x02'))
        Chunk(address=10, data=bytearray(b'\\x03\\x04'))
        Chunk(address=12, data=bytearray(b'\\x05'))

        Each segment can be split into smaller pieces using the
        `chunks(size=32, alignment=1)` method on a single segment.

        >>> for segment in binfile.segments:
        ...     print(segment)
        ...     for chunk in segment.chunks(2):
        ...         print(chunk)
        ...
        Segment(address=0, data=bytearray(b'\\x00\\x01\\x02'))
        Chunk(address=0, data=bytearray(b'\\x00\\x01'))
        Chunk(address=2, data=bytearray(b'\\x02'))
        Segment(address=10, data=bytearray(b'\\x03\\x04\\x05'))
        Chunk(address=10, data=bytearray(b'\\x03\\x04'))
        Chunk(address=12, data=bytearray(b'\\x05'))

        """

        return self._segments

    def add(self, data, overwrite=False):
        """Add given data string by guessing its format. The format must be
        Motorola S-Records, Intel HEX or TI-TXT. Set `overwrite` to
        ``True`` to allow already added data to be overwritten.

        """

        if is_srec(data):
            self.add_srec(data, overwrite)
        elif is_ihex(data):
            self.add_ihex(data, overwrite)
        elif is_ti_txt(data):
            self.add_ti_txt(data, overwrite)
        else:
            raise UnsupportedFileFormatError()

    def add_srec(self, records, overwrite=False):
        """Add given Motorola S-Records string. Set `overwrite` to ``True`` to
        allow already added data to be overwritten.

        """

        for record in StringIO(records):
            record = record.strip()

            # Ignore blank lines.
            if not record:
                continue

            type_, address, size, data = unpack_srec(record)

            if type_ == '0':
                self._header = data
            elif type_ in '123':
                address *= self.word_size_bytes
                self._segments.add(_Segment(address,
                                            address + size,
                                            bytearray(data),
                                            self.word_size_bytes),
                                   overwrite)
            elif type_ in '789':
                self.execution_start_address = address

    def add_ihex(self, records, overwrite=False):
        """Add given Intel HEX records string. Set `overwrite` to ``True`` to
        allow already added data to be overwritten.

        """

        extended_segment_address = 0
        extended_linear_address = 0

        for record in StringIO(records):
            record = record.strip()

            # Ignore blank lines.
            if not record:
                continue

            type_, address, size, data = unpack_ihex(record)

            if type_ == IHEX_DATA:
                address = (address
                           + extended_segment_address
                           + extended_linear_address)
                address *= self.word_size_bytes
                self._segments.add(_Segment(address,
                                            address + size,
                                            bytearray(data),
                                            self.word_size_bytes),
                                   overwrite)
            elif type_ == IHEX_END_OF_FILE:
                pass
            elif type_ == IHEX_EXTENDED_SEGMENT_ADDRESS:
                extended_segment_address = int(binascii.hexlify(data), 16)
                extended_segment_address *= 16
            elif type_ == IHEX_EXTENDED_LINEAR_ADDRESS:
                extended_linear_address = int(binascii.hexlify(data), 16)
                extended_linear_address <<= 16
            elif type_ in [IHEX_START_SEGMENT_ADDRESS, IHEX_START_LINEAR_ADDRESS]:
                self.execution_start_address = int(binascii.hexlify(data), 16)
            else:
                raise Error("expected type 1..5 in record {}, but got {}".format(
                    record,
                    type_))

    def add_ti_txt(self, lines, overwrite=False):
        """Add given TI-TXT string `lines`. Set `overwrite` to ``True`` to
        allow already added data to be overwritten.

        """

        address = None
        eof_found = False

        for line in StringIO(lines):
            # Abort if data is found after end of file.
            if eof_found:
                raise Error("bad file terminator")

            line = line.strip()

            if len(line) < 1:
                raise Error("bad line length")

            if line[0] == 'q':
                eof_found = True
            elif line[0] == '@':
                try:
                    address = int(line[1:], 16)
                except ValueError:
                    raise Error("bad section address")
            else:
                # Try to decode the data.
                try:
                    data = bytearray(binascii.unhexlify(line.replace(' ', '')))
                except (TypeError, binascii.Error):
                    raise Error("bad data")

                size = len(data)

                # Check that there are correct number of bytes per
                # line. There should TI_TXT_BYTES_PER_LINE. Only
                # exception is last line of section which may be
                # shorter.
                if size > TI_TXT_BYTES_PER_LINE:
                    raise Error("bad line length")

                if address is None:
                    raise Error("missing section address")

                self._segments.add(_Segment(address,
                                            address + size,
                                            data,
                                            self.word_size_bytes),
                                   overwrite)

                if size == TI_TXT_BYTES_PER_LINE:
                    address += size
                else:
                    address = None

        if not eof_found:
            raise Error("missing file terminator")

    def add_binary(self, data, address=0, overwrite=False):
        """Add given data at given address. Set `overwrite` to ``True`` to
        allow already added data to be overwritten.

        """

        address *= self.word_size_bytes
        self._segments.add(_Segment(address,
                                    address + len(data),
                                    bytearray(data),
                                    self.word_size_bytes),
                           overwrite)

    def add_file(self, filename, overwrite=False):
        """Open given file and add its data by guessing its format. The format
        must be Motorola S-Records, Intel HEX or TI-TXT. Set `overwrite` to
        ``True`` to allow already added data to be overwritten.

        """

        with open(filename, 'r') as fin:
            self.add(fin.read(), overwrite)

    def add_srec_file(self, filename, overwrite=False):
        """Open given Motorola S-Records file and add its records. Set
        `overwrite` to ``True`` to allow already added data to be
        overwritten.

        """

        with open(filename, 'r') as fin:
            self.add_srec(fin.read(), overwrite)

    def add_ihex_file(self, filename, overwrite=False):
        """Open given Intel HEX file and add its records. Set `overwrite` to
        ``True`` to allow already added data to be overwritten.

        """

        with open(filename, 'r') as fin:
            self.add_ihex(fin.read(), overwrite)

    def add_ti_txt_file(self, filename, overwrite=False):
        """Open given TI-TXT file and add its contents. Set `overwrite` to
        ``True`` to allow already added data to be overwritten.

        """

        with open(filename, 'r') as fin:
            self.add_ti_txt(fin.read(), overwrite)

    def add_binary_file(self, filename, address=0, overwrite=False):
        """Open given binary file and add its contents. Set `overwrite` to
        ``True`` to allow already added data to be overwritten.

        """

        with open(filename, 'rb') as fin:
            self.add_binary(fin.read(), address, overwrite)

    def as_srec(self, number_of_data_bytes=32, address_length_bits=32):
        """Format the binary file as Motorola S-Records records and return
        them as a string.

        `number_of_data_bytes` is the number of data bytes in each
        record.

        `address_length_bits` is the number of address bits in each
        record.

        >>> print(binfile.as_srec())
        S32500000100214601360121470136007EFE09D219012146017E17C20001FF5F16002148011973
        S32500000120194E79234623965778239EDA3F01B2CA3F0156702B5E712B722B73214601342199
        S5030002FA

        """

        header = []

        if self._header is not None:
            record = pack_srec('0', 0, len(self._header), self._header)
            header.append(record)

        type_ = str((address_length_bits // 8) - 1)

        if type_ not in '123':
            raise Error("expected data record type 1..3, but got {}".format(
                type_))

        data = [pack_srec(type_, address, len(data), data)
                for address, data in self._segments.chunks(number_of_data_bytes)]
        number_of_records = len(data)

        if number_of_records <= 0xffff:
            footer = [pack_srec('5', number_of_records, 0, None)]
        elif number_of_records <= 0xffffff:
            footer = [pack_srec('6', number_of_records, 0, None)]
        else:
            raise Error('too many records {}'.format(number_of_records))

        # Add the execution start address.
        if self.execution_start_address is not None:
            if type_ == '1':
                record = pack_srec('9', self.execution_start_address, 0, None)
            elif type_ == '2':
                record = pack_srec('8', self.execution_start_address, 0, None)
            else:
                record = pack_srec('7', self.execution_start_address, 0, None)

            footer.append(record)

        return '\n'.join(header + data + footer) + '\n'

    def as_ihex(self, number_of_data_bytes=32, address_length_bits=32):
        """Format the binary file as Intel HEX records and return them as a
        string.

        `number_of_data_bytes` is the number of data bytes in each
        record.

        `address_length_bits` is the number of address bits in each
        record.

        >>> print(binfile.as_ihex())
        :20010000214601360121470136007EFE09D219012146017E17C20001FF5F16002148011979
        :20012000194E79234623965778239EDA3F01B2CA3F0156702B5E712B722B7321460134219F
        :00000001FF

        """

        def i32hex(address, extended_linear_address, data_address):
            if address > 0xffffffff:
                raise Error(
                    'cannot address more than 4 GB in I32HEX files (32 '
                    'bits addresses)')

            address_upper_16_bits = (address >> 16)
            address &= 0xffff

            # All segments are sorted by address. Update the
            # extended linear address when required.
            if address_upper_16_bits > extended_linear_address:
                extended_linear_address = address_upper_16_bits
                packed = pack_ihex(IHEX_EXTENDED_LINEAR_ADDRESS,
                                   0,
                                   2,
                                   binascii.unhexlify('{:04X}'.format(
                                       extended_linear_address)))
                data_address.append(packed)

            return address, extended_linear_address

        def i16hex(address, extended_segment_address, data_address):
            if address > 16 * 0xffff + 0xffff:
                raise Error(
                    'cannot address more than 1 MB in I16HEX files (20 '
                    'bits addresses)')

            address_lower = (address - 16 * extended_segment_address)

            # All segments are sorted by address. Update the
            # extended segment address when required.
            if address_lower > 0xffff:
                extended_segment_address = (4096 * (address >> 16))

                if extended_segment_address > 0xffff:
                    extended_segment_address = 0xffff

                address_lower = (address - 16 * extended_segment_address)
                packed = pack_ihex(IHEX_EXTENDED_SEGMENT_ADDRESS,
                                   0,
                                   2,
                                   binascii.unhexlify('{:04X}'.format(
                                       extended_segment_address)))
                data_address.append(packed)

            return address_lower, extended_segment_address

        def i8hex(address):
            if address > 0xffff:
                raise Error(
                    'cannot address more than 64 kB in I8HEX files (16 '
                    'bits addresses)')

        data_address = []
        extended_segment_address = 0
        extended_linear_address = 0

        for address, data in self._segments.chunks(number_of_data_bytes):
            if address_length_bits == 32:
                address, extended_linear_address = i32hex(address,
                                                          extended_linear_address,
                                                          data_address)
            elif address_length_bits == 24:
                address, extended_segment_address = i16hex(address,
                                                           extended_segment_address,
                                                           data_address)
            elif address_length_bits == 16:
                i8hex(address)
            else:
                raise Error(
                    'expected address length 16, 24 or 32, but got {}'.format(
                        address_length_bits))

            data_address.append(pack_ihex(IHEX_DATA,
                                          address,
                                          len(data),
                                          data))

        footer = []

        if self.execution_start_address is not None:
            if address_length_bits == 24:
                address = binascii.unhexlify(
                    '{:08X}'.format(self.execution_start_address))
                footer.append(pack_ihex(IHEX_START_SEGMENT_ADDRESS,
                                        0,
                                        4,
                                        address))
            elif address_length_bits == 32:
                address = binascii.unhexlify(
                    '{:08X}'.format(self.execution_start_address))
                footer.append(pack_ihex(IHEX_START_LINEAR_ADDRESS,
                                        0,
                                        4,
                                        address))

        footer.append(pack_ihex(IHEX_END_OF_FILE, 0, 0, None))

        return '\n'.join(data_address + footer) + '\n'

    def as_ti_txt(self):
        """Format the binary file as a TI-TXT file and return it as a string.

        >>> print(binfile.as_ti_txt())
        @0100
        21 46 01 36 01 21 47 01 36 00 7E FE 09 D2 19 01
        21 46 01 7E 17 C2 00 01 FF 5F 16 00 21 48 01 19
        19 4E 79 23 46 23 96 57 78 23 9E DA 3F 01 B2 CA
        3F 01 56 70 2B 5E 71 2B 72 2B 73 21 46 01 34 21
        q

        """

        lines = []

        for segment in self._segments:
            lines.append('@{:04X}'.format(segment.address))

            for _, data in segment.chunks(TI_TXT_BYTES_PER_LINE):
                lines.append(' '.join('{:02X}'.format(byte) for byte in data))

        lines.append('q')

        return '\n'.join(lines) + '\n'

    def as_binary(self,
                  minimum_address=None,
                  maximum_address=None,
                  padding=None):
        """Return a byte string of all data within given address range.

        `minimum_address` is the absolute minimum address of the
        resulting binary data. By default this is the minimum address
        in the binary.

        `maximum_address` is the absolute maximum address of the
        resulting binary data (non-inclusive). By default this is the
        maximum address in the binary.

        `padding` is the word value of the padding between
        non-adjacent segments. Give as a bytes object of length 1 when
        the word size is 8 bits, length 2 when the word size is 16
        bits, and so on. By default the padding is ``b'\\xff' *
        word_size_bytes``.

        >>> binfile.as_binary()
        bytearray(b'!F\\x016\\x01!G\\x016\\x00~\\xfe\\t\\xd2\\x19\\x01!F\\x01~\\x17\\xc2\\x00\\x01
        \\xff_\\x16\\x00!H\\x01\\x19\\x19Ny#F#\\x96Wx#\\x9e\\xda?\\x01\\xb2\\xca?\\x01Vp+^q+r+s!
        F\\x014!')

        """

        if len(self._segments) == 0:
            return b''

        if minimum_address is None:
            current_maximum_address = self.minimum_address
        else:
            current_maximum_address = minimum_address

        if maximum_address is None:
            maximum_address = self.maximum_address

        if current_maximum_address >= maximum_address:
            return b''

        if padding is None:
            padding = b'\xff' * self.word_size_bytes

        binary = bytearray()

        for address, data in self._segments:
            length = len(data) // self.word_size_bytes

            # Discard data below the minimum address.
            if address < current_maximum_address:
                if address + length <= current_maximum_address:
                    continue

                offset = (current_maximum_address - address) * self.word_size_bytes
                data = data[offset:]
                length = len(data) // self.word_size_bytes
                address = current_maximum_address

            # Discard data above the maximum address.
            if address + length > maximum_address:
                if address < maximum_address:
                    size = (maximum_address - address) * self.word_size_bytes
                    data = data[:size]
                    length = len(data) // self.word_size_bytes
                elif maximum_address >= current_maximum_address:
                    binary += padding * (maximum_address - current_maximum_address)
                    break

            binary += padding * (address - current_maximum_address)
            binary += data
            current_maximum_address = address + length

        return binary

    def as_array(self, minimum_address=None, padding=None, separator=', '):
        """Format the binary file as a string values separated by given
        separator `separator`. This function can be used to generate
        array initialization code for C and other languages.

        `minimum_address` is the absolute minimum address of the
        resulting binary data. By default this is the minimum address
        in the binary.

        `padding` is the word value of the padding between
        non-adjacent segments. Give as a bytes object of length 1 when
        the word size is 8 bits, length 2 when the word size is 16
        bits, and so on. By default the padding is ``b'\\xff' *
        word_size_bytes``.

        >>> binfile.as_array()
        '0x21, 0x46, 0x01, 0x36, 0x01, 0x21, 0x47, 0x01, 0x36, 0x00, 0x7e,
         0xfe, 0x09, 0xd2, 0x19, 0x01, 0x21, 0x46, 0x01, 0x7e, 0x17, 0xc2,
         0x00, 0x01, 0xff, 0x5f, 0x16, 0x00, 0x21, 0x48, 0x01, 0x19, 0x19,
         0x4e, 0x79, 0x23, 0x46, 0x23, 0x96, 0x57, 0x78, 0x23, 0x9e, 0xda,
         0x3f, 0x01, 0xb2, 0xca, 0x3f, 0x01, 0x56, 0x70, 0x2b, 0x5e, 0x71,
         0x2b, 0x72, 0x2b, 0x73, 0x21, 0x46, 0x01, 0x34, 0x21'

        """

        binary_data = self.as_binary(minimum_address,
                                     padding=padding)
        words = []

        for offset in range(0, len(binary_data), self.word_size_bytes):
            word = 0

            for byte in binary_data[offset:offset + self.word_size_bytes]:
                word <<= 8
                word += byte

            words.append('0x{:02x}'.format(word))

        return separator.join(words)

    def as_hexdump(self):
        """Format the binary file as a hexdump and return it as a string.

        >>> print(binfile.as_hexdump())
        00000100  21 46 01 36 01 21 47 01  36 00 7e fe 09 d2 19 01  |!F.6.!G.6.~.....|
        00000110  21 46 01 7e 17 c2 00 01  ff 5f 16 00 21 48 01 19  |!F.~....._..!H..|
        00000120  19 4e 79 23 46 23 96 57  78 23 9e da 3f 01 b2 ca  |.Ny#F#.Wx#..?...|
        00000130  3f 01 56 70 2b 5e 71 2b  72 2b 73 21 46 01 34 21  |?.Vp+^q+r+s!F.4!|

        """

        # Empty file?
        if len(self) == 0:
            return '\n'

        non_dot_characters = set(string.printable)
        non_dot_characters -= set(string.whitespace)
        non_dot_characters |= set(' ')

        def align16(address):
            return address - (address % 16)

        def padding(length):
            return [None] * length

        def format_line(address, data):
            """`data` is a list of integers and None for unused elements.

            """

            data += padding(16 - len(data))
            hexdata = []

            for byte in data:
                if byte is not None:
                    elem = '{:02x}'.format(byte)
                else:
                    elem = '  '

                hexdata.append(elem)

            first_half = ' '.join(hexdata[0:8])
            second_half = ' '.join(hexdata[8:16])
            text = ''

            for byte in data:
                if byte is None:
                    text += ' '
                elif chr(byte) in non_dot_characters:
                    text += chr(byte)
                else:
                    text += '.'

            return '{:08x}  {:23s}  {:23s}  |{:16s}|'.format(
                address, first_half, second_half, text)

        # Format one line at a time.
        lines = []
        line_address = align16(self.minimum_address)
        line_data = []

        for chunk in self._segments.chunks(size=16, alignment=16):
            aligned_chunk_address = align16(chunk.address)

            if aligned_chunk_address > line_address:
                lines.append(format_line(line_address, line_data))

                if aligned_chunk_address > line_address + 16:
                    lines.append('...')

                line_address = aligned_chunk_address
                line_data = []

            line_data += padding(chunk.address - line_address - len(line_data))
            line_data += [byte for byte in chunk.data]

        lines.append(format_line(line_address, line_data))

        return '\n'.join(lines) + '\n'

    def fill(self, value=None, max_words=None):
        """Fill all empty space between segments.

        `value` is the value which is used to fill the empty space. By
        default the value is ``b'\\xff' * word_size_bytes``.

        `max_words` is the maximum number of words to fill between the
        segments. Empty space which larger than this is not
        touched. If ``None``, all empty space is filled.

        """

        if value is None:
            value = b'\xff' * self.word_size_bytes

        previous_segment_maximum_address = None
        fill_segments = []

        for address, data in self._segments:
            address *= self.word_size_bytes
            maximum_address = address + len(data)

            if previous_segment_maximum_address is not None:
                fill_size = address - previous_segment_maximum_address
                fill_size_words = fill_size // self.word_size_bytes

                if max_words is None or fill_size_words <= max_words:
                    fill_segments.append(_Segment(
                        previous_segment_maximum_address,
                        previous_segment_maximum_address + fill_size,
                        value * fill_size_words,
                        self.word_size_bytes))

            previous_segment_maximum_address = maximum_address

        for segment in fill_segments:
            self._segments.add(segment)

    def exclude(self, minimum_address, maximum_address):
        """Exclude given range and keep the rest.

        `minimum_address` is the first word address to exclude
        (including).

        `maximum_address` is the last word address to exclude
        (excluding).

        """

        if maximum_address < minimum_address:
            raise Error('bad address range')

        minimum_address *= self.word_size_bytes
        maximum_address *= self.word_size_bytes
        self._segments.remove(minimum_address, maximum_address)

    def crop(self, minimum_address, maximum_address):
        """Keep given range and discard the rest.

        `minimum_address` is the first word address to keep
        (including).

        `maximum_address` is the last word address to keep
        (excluding).

        """

        minimum_address *= self.word_size_bytes
        maximum_address *= self.word_size_bytes
        maximum_address_address = self._segments.maximum_address
        self._segments.remove(0, minimum_address)
        self._segments.remove(maximum_address, maximum_address_address)

    def info(self):
        """Return a string of human readable information about the binary
        file.

        .. code-block:: python

           >>> print(binfile.info())
           Data ranges:

               0x00000100 - 0x00000140 (64 bytes)

        """

        info = ''

        if self._header is not None:
            if self._header_encoding is None:
                header = ''

                for b in bytearray(self.header):
                    if chr(b) in string.printable:
                        header += chr(b)
                    else:
                        header += '\\x{:02x}'.format(b)
            else:
                header = self.header

            info += 'Header:                  "{}"\n'.format(header)

        if self.execution_start_address is not None:
            info += 'Execution start address: 0x{:08x}\n'.format(
                self.execution_start_address)

        info += 'Data ranges:\n\n'

        for address, data in self._segments:
            minimum_address = address
            size = len(data)
            maximum_address = (minimum_address + size // self.word_size_bytes)
            info += 4 * ' '
            info += '0x{:08x} - 0x{:08x} ({})\n'.format(
                minimum_address,
                maximum_address,
                format_size(size, binary=True))

        return info


def _do_info(args):
    for binfile in args.binfile:
        bf = BinFile(header_encoding=args.header_encoding,
                     word_size_bits=args.word_size_bits)
        bf.add_file(binfile)
        print(bf.info())


def _convert_input_format_type(value):
    items = value.split(',')
    fmt = items[0]
    args = tuple()

    if fmt == 'binary':
        address = 0

        if len(items) >= 2:
            try:
                address = int(items[1], 0)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "invalid binary address '{}'".format(items[1]))

        args = (address, )
    elif fmt in ['ihex', 'srec', 'auto', 'ti_txt']:
        pass
    else:
        raise argparse.ArgumentTypeError("invalid input format '{}'".format(fmt))

    return fmt, args


def _convert_output_format_type(value):
    items = value.split(',')
    fmt = items[0]
    args = tuple()

    if fmt in ['srec', 'ihex', 'ti_txt']:
        number_of_data_bytes = 32
        address_length_bits = 32

        if len(items) >= 2:
            try:
                number_of_data_bytes = int(items[1], 0)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "invalid {} number of data bytes '{}'".format(fmt, items[1]))

        if len(items) >= 3:
            try:
                address_length_bits = int(items[2], 0)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "invalid {} address length of '{}' bits".format(fmt, items[2]))

        args = (number_of_data_bytes, address_length_bits)
    elif fmt == 'binary':
        minimum_address = None
        maximum_address = None

        if len(items) >= 2:
            try:
                minimum_address = int(items[1], 0)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "invalid binary minimum address '{}'".format(items[1]))

        if len(items) >= 3:
            try:
                maximum_address = int(items[2], 0)
            except ValueError:
                raise argparse.ArgumentTypeError(
                    "invalid binary maximum address '{}'".format(items[2]))

        args = (minimum_address, maximum_address)
    elif fmt == 'hexdump':
        pass
    else:
        raise argparse.ArgumentTypeError("invalid output format '{}'".format(fmt))

    return fmt, args


def _do_convert_add_file(bf, input_format, infile, overwrite):
    fmt, args = input_format

    try:
        if fmt == 'auto':
            try:
                bf.add_file(infile, *args, overwrite=overwrite)
            except UnsupportedFileFormatError:
                bf.add_binary_file(infile, *args, overwrite=overwrite)
        elif fmt == 'srec':
            bf.add_srec_file(infile, *args, overwrite=overwrite)
        elif fmt == 'ihex':
            bf.add_ihex_file(infile, *args, overwrite=overwrite)
        elif fmt == 'binary':
            bf.add_binary_file(infile, *args, overwrite=overwrite)
        elif fmt == 'ti_txt':
            bf.add_ti_txt_file(infile, *args, overwrite=overwrite)
    except AddDataError:
        sys.exit('overlapping segments detected, give --overwrite to overwrite '
                 'overlapping segments')


def _do_convert_as(bf, output_format):
    fmt, args = output_format

    if fmt == 'srec':
        converted = bf.as_srec(*args)
    elif fmt == 'ihex':
        converted = bf.as_ihex(*args)
    elif fmt == 'binary':
        converted = bf.as_binary(*args)
    elif fmt == 'hexdump':
        converted = bf.as_hexdump()
    elif fmt == 'ti_txt':
        converted = bf.as_ti_txt()

    return converted


def _do_convert(args):
    input_formats_missing = len(args.infiles) - len(args.input_format)

    if input_formats_missing < 0:
        sys.exit("found more input formats than input files")

    args.input_format += input_formats_missing * [('auto', tuple())]
    binfile = BinFile(word_size_bits=args.word_size_bits)

    for input_format, infile in zip(args.input_format, args.infiles):
        _do_convert_add_file(binfile, input_format, infile, args.overwrite)

    converted = _do_convert_as(binfile, args.output_format)

    if args.outfile == '-':
        if isinstance(converted, str):
            print(converted, end='')
        else:
            if sys.version_info[0] >= 3:
                sys.stdout.buffer.write(converted)
            else:
                sys.stdout.write(converted)
    else:
        if isinstance(converted, str):
            with open(args.outfile, 'w') as fout:
                fout.write(converted)
        else:
            with open(args.outfile, 'wb') as fout:
                fout.write(converted)


def _do_as_srec(args):
    for binfile in args.binfile:
        bf = BinFile()
        bf.add_file(binfile)
        print(bf.as_srec(), end='')


def _do_as_ihex(args):
    for binfile in args.binfile:
        bf = BinFile()
        bf.add_file(binfile)
        print(bf.as_ihex(), end='')


def _do_as_hexdump(args):
    for binfile in args.binfile:
        bf = BinFile()
        bf.add_file(binfile)
        print(bf.as_hexdump(), end='')

def _do_as_ti_txt(args):
    for binfile in args.binfile:
        bf = BinFile()
        bf.add_file(binfile)
        print(bf.as_ti_txt(), end='')


def _main():
    parser = argparse.ArgumentParser(
        description='Various binary file format utilities.')

    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('--version',
                        action='version',
                        version=__version__,
                        help='Print version information and exit.')

    # Workaround to make the subparser required in Python 3.
    subparsers = parser.add_subparsers(title='subcommands',
                                       dest='subcommand')
    subparsers.required = True

    # The 'info' subparser.
    subparser = subparsers.add_parser(
        'info',
        description='Print general information about given file(s).')
    subparser.add_argument('-e', '--header-encoding',
                           help=('File header encoding. Common encodings '
                                 'include utf-8 and ascii.'))
    subparser.add_argument(
        '-s', '--word-size-bits',
        default=8,
        type=int,
        help='Word size in number of bits (default: %(default)s).')
    subparser.add_argument('binfile',
                           nargs='+',
                           help='One or more binary format files.')
    subparser.set_defaults(func=_do_info)

    # The 'convert' subparser.
    subparser = subparsers.add_parser(
        'convert',
        description='Convert given file(s) to a single file.')
    subparser.add_argument(
        '-i', '--input-format',
        action='append',
        default=[],
        type=_convert_input_format_type,
        help=('Input format auto, srec, ihex, ti_txt, or binary (default: auto). This '
              'argument may be repeated, selecting the input format for each '
              'input file.'))
    subparser.add_argument(
        '-o', '--output-format',
        default='hexdump',
        type=_convert_output_format_type,
        help=('Output format srec, ihex, ti_txt, binary or hexdump '
              '(default: %(default)s).'))
    subparser.add_argument(
        '-s', '--word-size-bits',
        default=8,
        type=int,
        help='Word size in number of bits (default: %(default)s).')
    subparser.add_argument('-w', '--overwrite',
                           action='store_true',
                           help='Overwrite overlapping data segments.')
    subparser.add_argument('infiles',
                           nargs='+',
                           help='One or more binary format files.')
    subparser.add_argument('outfile',
                           help='Output file, or - to print to standard output.')
    subparser.set_defaults(func=_do_convert)

    # The 'as_srec' subparser.
    subparser = subparsers.add_parser(
        'as_srec',
        description='Print given file(s) as Motorola S-records.')
    subparser.add_argument('binfile',
                           nargs='+',
                           help='One or more binary format files.')
    subparser.set_defaults(func=_do_as_srec)

    # The 'as_ihex' subparser.
    subparser = subparsers.add_parser(
        'as_ihex',
        description='Print given file(s) as Intel HEX.')
    subparser.add_argument('binfile',
                           nargs='+',
                           help='One or more binary format files.')
    subparser.set_defaults(func=_do_as_ihex)

    # The 'as_hexdump' subparser.
    subparser = subparsers.add_parser(
        'as_hexdump',
        description='Print given file(s) as hexdumps.')
    subparser.add_argument('binfile',
                           nargs='+',
                           help='One or more binary format files.')
    subparser.set_defaults(func=_do_as_hexdump)

    # The 'as_ti_txt' subparser.
    subparser = subparsers.add_parser(
        'as_ti_txt',
        description='Print given file(s) as TI-TXT.')
    subparser.add_argument('binfile',
                           nargs='+',
                           help='One or more binary format files.')
    subparser.set_defaults(func=_do_as_ti_txt)

    args = parser.parse_args()

    if args.debug:
        args.func(args)
    else:
        try:
            args.func(args)
        except BaseException as e:
            sys.exit('error: ' + str(e))


if __name__ == '__main__':
    _main()
