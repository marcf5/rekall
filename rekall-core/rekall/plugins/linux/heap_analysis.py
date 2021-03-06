#  glibc heap analysis classes
#
#    Copyright (c) 2017, Frank Block, ERNW GmbH <fblock@ernw.de>
#
#       All rights reserved.
#
#       Redistribution and use in source and binary forms, with or without modification,
#       are permitted provided that the following conditions are met:
#
#       * Redistributions of source code must retain the above copyright notice, this
#         list of conditions and the following disclaimer.
#       * Redistributions in binary form must reproduce the above copyright notice,
#         this list of conditions and the following disclaimer in the documentation
#         and/or other materials provided with the distribution.
#       * The names of the contributors may not be used to endorse or promote products
#         derived from this software without specific prior written permission.
#
#       THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#       AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#       IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#       ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#       LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
#       DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
#       SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
#       CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
#       OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#       OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""
This module implements several classes, allowing the glibc heap analysis for a
given process.
"""
from __future__ import print_function

from builtins import str
from builtins import hex
from builtins import range
import re
import pdb
import struct
import traceback
import os
from numbers import Number
from rekall.plugins.overlays import basic
from rekall.plugins.linux import common
from rekall.plugins.linux import cpuinfo
from rekall.plugins import core
from rekall import scan
from rekall import obj

#############

_PREV_INUSE = 0x1
_IS_MMAPPED = 0x2
_NON_MAIN_ARENA = 0x4
_SIZE_BITS = (_PREV_INUSE | _IS_MMAPPED | _NON_MAIN_ARENA)
# is set on HeapAnalysis instantiation
_MIN_LARGE_SIZE = None

# Probably more versions would work, especially when the corresponding vtype
# information are provided, but those are the versions we tested against
_SUPPORTED_GLIBC_VERSIONS = ['2.25', '2.24', '2.23', '2.22', '2.21', '2.20']
_LIBC_REGEX = '(?:^|/)libc[^a-zA-Z][^/]*\\.so'


def get_vma_for_offset(vmas, offset):
    """Returns a list with identifier and vm_area that given offset belongs to.
    Expects the output from _get_vmas_for_task as argument.
    """

    for vma in vmas:
        if vma['vma'].vm_start <= offset < vma['vma'].vm_end:
            return vma

    return None


def get_libc_filename(vmas):
    """Returns the libc file name from the vma, where the _LIBC_REGEX matches.
    """

    if vmas:
        for vma in vmas:
            if re.search(_LIBC_REGEX, vma['name'], re.IGNORECASE):
                return vma['name']


def get_libc_range(vmas):
    """Returns the lowest and highest address for the libc vma. See also
    get_mem_range_for_regex."""

    return get_mem_range_for_regex(vmas, _LIBC_REGEX)


def get_mem_range_for_regex(vmas, regex):
    """Returns the lowest and highest address of memory areas belonging to the
    vm_areas, the given regex matches on. The result is given as a list, where
    the lowest address is the first element. Expects the output from
    _get_vmas_for_task as argument."""

    offsets = None
    if vmas:
        for vma in vmas:
            if re.search(regex, vma['name'], re.IGNORECASE):
                if not offsets:
                    offsets = [vma['vma'].vm_start]
                    offsets.append(vma['vma'].vm_end)

                else:
                    offsets[1] = vma['vma'].vm_end

    return offsets



class HeapAnalysis(common.LinProcessFilter):
    """Basic abstract class for linux heap analysis.
        Mostly serves the main_arena.
    """

    __abstract = True

    _main_heap_identifier = "[heap]"

    # used to mark vm_areas residing between the [heap] and the first file or
    # stack object and all other vm_areas that have no file object for
    # (mmapped regions can also reside somewhere beyond the typical heap/stack
    # area) note that vmas with this identifier might be empty, old thread
    # stacks or vm_areas belonging to e.g. mapped files.
    # A pretty reliable source for heap vm_areas is the self.heap_vmas list as
    # it contains vm_areas which are identified to most probably belong to a
    # heap or mmapped region.
    _heap_vma_identifier = "[heap-vma]"
    _pot_mmapped_vma_identifier = "[pot-mmapped-vma]"


    # is normally only automatically set when using a dummy arena or the chunk
    # dumper, as in those cases all chunks are walked at least two times
    def activate_chunk_preservation(self):
        """Sets _preserve_chunks to True. This forces all allocated chunk
        functions to store chunks in lists, which highly increases the speed
        of a second walk over those chunks. This feature can only be activated
        if performance is set to 'fast'."""

        if not self._preserve_chunks and \
                self.session.GetParameter("performance") == "fast":
            self.session.logging.warn(
                "Chunk preservation has been activated (result from using "
                "performance=fast). This might consume large amounts of memory"
                " depending on the chunk count. If you are low on free memory "
                "space (RAM), you might want to deactivate this feature by "
                "not using the 'fast' option. The only downside is in some "
                "cases a longer plugin runtime.")

            self._preserve_chunks = True


    def _get_saved_stack_frame_pointers(self, task):
        """Returns a list of dicts, containing the ebp,esp and pid values
        for each thread."""

        if not task.mm:
            return None

        # To gather thread stacks, we examine the pt_regs struct for each
        # thread and extract the saved stack frame pointers
        thread_stack_offsets = []
        thread_group_offset = self.profile.get_obj_offset("task_struct",
                                                          "thread_group")

        for thread_group in task.thread_group.walk_list("next"):
            thread_task = self.profile.task_struct(
                offset=thread_group.obj_offset - thread_group_offset,
                vm=self.process_as)

            pt_regs = self.profile.pt_regs(
                offset=(thread_task.thread.sp0 -
                        self.profile.get_obj_size("pt_regs")),
                vm=self.process_as)

            thread_stack_offsets.append(dict(ebp=pt_regs.bp.v(),
                                             esp=pt_regs.sp.v(),
                                             pid=thread_task.pid.v()))

        return thread_stack_offsets


    # Basically the code from the proc_maps plugin but with thread specific
    # enhancements
    def _get_vmas_for_task(self, task):
        """Returns a list of lists, containing ["name", vm_area]  pairs. """

        if not task.mm:
            return None

        result = []

        thread_stack_offsets = self._get_saved_stack_frame_pointers(task)
        # The first pair contains the "main" thread and the mm start_stack
        # value is more reliable for identifying the relevant memory region
        # than the saved frame pointers
        thread_stack_offsets[0]['start_stack'] = task.mm.start_stack

        heap_area = False
        for vma in task.mm.mmap.walk_list("vm_next"):
            temp_vma = dict()

            if vma.vm_file:
                fname = task.get_path(vma.vm_file)
                if heap_area:
                    heap_area = False

            else:
                fname = ""
                if heap_area:
                    fname = self._heap_vma_identifier

                else:
                    fname = self._pot_mmapped_vma_identifier


                # main heap can have 3 or more vm_area_struct structs
                if vma.vm_start <= task.mm.start_brk <= vma.vm_end or \
                        (task.mm.start_brk <= vma.vm_start
                         < vma.vm_end <= task.mm.brk) or \
                        vma.vm_start <= task.mm.brk <= vma.vm_end:
                    fname = self._main_heap_identifier
                    heap_area = True

                else:
                    for offsets in thread_stack_offsets:
                        if (('start_stack' in list(offsets.keys()) and
                             vma.vm_start <= offsets['start_stack']
                             <= vma.vm_end) or
                                vma.vm_start <= offsets['ebp'] <= vma.vm_end or
                                vma.vm_start <= offsets['esp'] <= vma.vm_end):

                            fname = "[stack"
                            pid = offsets['pid']
                            fname += "]" if task.pid == pid else \
                                     ":{:d}]".format(pid)

                            temp_vma['ebp'] = offsets['ebp']
                            temp_vma['esp'] = offsets['esp']

                            heap_area = False

            temp_vma['name'] = fname
            temp_vma['vma'] = vma
            result.append(temp_vma)

        return sorted(result, key=lambda vma: vma['vma'].vm_start)


    def _load_libc_profile(self):
        """Loads the Libc profile for the current libc version."""

        # we try to gather version information from the mapped libc lib
        libc_version_string = None
        major_version = None
        minor_version = None
        match = None
        libc_filename = get_libc_filename(self.vmas)

        if libc_filename:
            match = re.search(r'(\d+)\.(\d+)', libc_filename)

        if match and len(match.groups()) == 2:
            major_version = int(match.group(1))
            minor_version = int(match.group(2))
            libc_version_string = str(major_version) + str(minor_version)

            self.session.logging.info("Trying to load profile for version {:s}"
                                      " from the repository."
                                      .format(libc_version_string))

            # TODO: dynamic selection of distribution specific profiles
            dist = 'base'
            libc_profile = self.session.LoadProfile(
                "glibc/{:s}/{:s}/{:s}".format(dist,
                                              self.profile.metadata("arch"),
                                              libc_version_string))


            if not libc_profile:
                # fallback: there seems to be no profile from the repository,
                # so we try to load a profile internally
                self.session.logging.info(
                    "Repository failed: Now using internal profiles.")

                # Fallback
                libc_version_string = '224'

                if major_version == 2:
                    if minor_version >= 24:
                        libc_version_string = '224'

                    elif minor_version == 23:
                        libc_version_string = '223'

                    else:
                        libc_version_string = '220'

                self.session.logging.info(
                    "Loading internal profile for version {:s}."
                    .format(libc_version_string))

                if self.session.profile.metadata("arch") == 'I386':
                    libc_profile = GlibcProfile32(version=libc_version_string,
                                                  session=self.session)

                elif self.session.profile.metadata("arch") == 'AMD64':
                    libc_profile = GlibcProfile64(version=libc_version_string,
                                                  session=self.session)

        if not libc_profile:
            self.session.logging.error('Unable to load a libc profile.')

        else:
            self.profile.add_types(libc_profile.vtypes)
            self.profile.add_constants(libc_profile.constants)

            if all(x in list(self.profile.vtypes.keys()) for x in
                   ['malloc_chunk', 'malloc_state',
                    'malloc_par', '_heap_info']):
                self._libc_profile_success = True

                self.profile.add_classes(dict(malloc_state=malloc_state,
                                              _heap_info=_heap_info,
                                              malloc_chunk=malloc_chunk))

            else:
                self.session.logging.error('Error while loading libc profile.')


    def _check_and_report_chunksize(self, chunk, current_border):
        """Checks whether or not the current chunk
                - is bigger than the given border
                - smaller than the minimum size a chunk is allowed to be
                - 's address is aligned.
        """

        if chunk.v() + chunk.chunksize() > current_border:
            self.session.logging.warn(
                "Chunk at offset 0x{:x} has a size larger than the current "
                "memory region. This shouldn't be the case."
                .format(chunk.v()))

            return False

        elif chunk.chunksize() < self._minsize:
            if not self._check_and_report_chunk_for_being_swapped(chunk):
                self.session.logging.warn(
                    "Chunk at offset 0x{:x} has a size smaller than MINSIZE, "
                    "which shouldn't be the case and indicates a problem."
                    .format(chunk.v()))

            return False

        elif not self._aligned_ok(chunk.chunksize()):
            self.session.logging.warn(
                "The size of chunk at offset 0x{:x} is not a multiple of "
                "MALLOC_ALIGNMENT, which shouldn't be the case and indicates "
                "a problem.".format(chunk.v()))

            return False

        return True


    # TODO reliable verification via page table information
    def _check_and_report_chunk_for_being_swapped(self, chunk):
        """Tests the size field of a given chunk for being 0. If this field
        is null, it is a good indication that the corresponding memory region
        has been swapped. The reason might however also be a calculation error
        for the chunk's offset."""

        if chunk:
            if chunk.get_size() == 0:
                self.session.logging.warn(
                    "It seems like the memory page(s) belonging to the "
                    "chunk at offset 0x{:x} have been swapped. This will lead "
                    "to incorrect/incomplete results and more warnings/errors."
                    .format(chunk.v()))
                return True

        return False



    def _check_and_report_allocated_chunk(
            self, arena, chunk, next_chunk, current_border):
        """Checks if the given chunk should be in use (depending on the
        PREV_INUSE bit of the next chunk), has a size > MINSIZE, is aligned,
        whether or not it is part of any bin or fastbin in conjunction with
        next_chunks PREV_INUSE bit and if next_chunks prev_size field has
        same value as current chunk's size. This function is not intended to
        be used for the "bottom chunks". It returns True if no error occurs
        and if the given chunk is not part of bins or fastbins.
        """

        error_base_string = (
            "Found a presumably {0} chunk at offset 0x{1:x} which is however "
            "{2}part of the bins. This is unexpected and might either "
            "indicate an error or possibly in seldom cases be the result "
            "from a race condition.")

        if not self._check_and_report_chunksize(chunk, current_border):
            return False

        if not self._aligned_ok(chunk.v()):
            self.session.logging.warn(
                "Chunk at offset 0x{:x} is not aligned. As chunks are normally"
                " always aligned, this indicates a mistakenly chosen chunk and"
                " probably results in wrong results.".format(chunk.v()))

            return False

        # current chunk is tested in _check_and_report_chunksize for
        # being swapped
        self._check_and_report_chunk_for_being_swapped(next_chunk)

        if next_chunk.prev_inuse():
            # for chunks in fastbins, the prev_inuse bit is not unset,
            # so we don't check that here
            if chunk in arena.freed_chunks:
                # freed chunks shouldn't be marked as in use
                self.session.logging.warn(
                    error_base_string.format("allocated", chunk.v(), "")
                )

            elif chunk not in arena.freed_fast_chunks:
                return True

        else:
            # current chunk seems to be freed, hence its size should equal
            # next chunk's prev_size
            if chunk.chunksize() != next_chunk.get_prev_size():
                self.session.logging.warn(
                    "Chunk at offset 0x{:x} seems to be freed but its size "
                    "doesn't match the next chunks prev_size value."
                    .format(chunk.v()))

            elif chunk in arena.freed_fast_chunks:
                # fastbins normally have the prev_inuse bit set
                self.session.logging.warn(
                    "Unexpected: Found fastbin-chunk at offset 0x{0:x} which "
                    "prev_inuse bit is unset. This shouldn't normally be the "
                    "case.".format(chunk.v()))

            elif chunk not in arena.freed_chunks:
                # chunk is not marked as in use, but neither part of any bin
                # or fastbin
                self.session.logging.warn(
                    error_base_string.format("freed", chunk.v(), "not ")
                )


        return False


    def _allocated_chunks_for_mmapped_chunk(self, mmap_first_chunk):
        """Returns all allocated chunks for the mmap region the given chunk
        belongs to."""

        if not mmap_first_chunk:
            self.session.logging.warn(
                "_allocated_chunks_for_mmapped_chunk has been called with "
                "invalid pointer.")
            return

        mmap_vma = get_vma_for_offset(self.vmas, mmap_first_chunk.v())['vma']
        current_border = mmap_vma.vm_end

        # we can't check here for hitting the bottom, as mmapped regions can
        # contain slack space but this test is in essence done in
        # check_and_report_mmap_chunk
        for curr_chunk in self.iterate_through_chunks(mmap_first_chunk,
                                                      current_border):

            if self._check_and_report_mmapped_chunk(curr_chunk, mmap_vma) \
                    and self._check_and_report_chunksize(curr_chunk,
                                                         current_border):
                yield curr_chunk

            else:
                # As the checks for the last MMAPPED chunk reported an error,
                # we are stopping walking the MMAPPED chunks for that vm_area.
                break



    def get_all_mmapped_chunks(self):
        """Returns all allocated MMAPPED chunks."""

        main_arena = self.get_main_arena()
        if main_arena:
            if main_arena.allocated_mmapped_chunks:
                for chunk in main_arena.allocated_mmapped_chunks:
                    yield chunk

                return

            main_arena.allocated_mmapped_chunks = list()

            for mmap_first_chunk in main_arena.mmapped_first_chunks:
                for chunk in self._allocated_chunks_for_mmapped_chunk(
                        mmap_first_chunk):
                    main_arena.allocated_mmapped_chunks.append(chunk)
                    yield chunk



    ######### code taken from malloc/malloc.c (glibc-2.23)
    # origins from the MINSIZE definition
    def get_aligned_address(self, address, different_align_mask=None):
        """Returns an aligned address or MINSIZE, if given MIN_CHUNK_SIZE as
        argument."""

        if different_align_mask:
            return (address + different_align_mask) & ~ different_align_mask

        return (address + self._malloc_align_mask) & ~ self._malloc_align_mask

    def _aligned_ok(self, value):
        """Returns True if the given address/size is aligned."""

        return (value & self._malloc_align_mask) == 0

    # essentially the request2size macro code
    def get_aligned_size(self, size):
        """Returns an aligned size. Originally used to align a user request
        size."""

        if size + self._size_sz + self._malloc_align_mask < self._minsize:
            return self._minsize & ~ self._malloc_align_mask

        return ((size + self._size_sz + self._malloc_align_mask)
                & ~ self._malloc_align_mask)

    ###########

    def _check_mmap_alignment(self, address):
        """Returns True if the given address is aligned according to the
        minimum pagesize."""

        return (address & (self._min_pagesize - 1)) == 0


    def _get_page_aligned_address(self, address):
        """Returns an address aligned to the internal pagesize.
        The given address should be a number, not a chunk.
        This function is primarily used in the context of MMAPPED chunks.
        """

        return (address + self._min_pagesize - 1) & ~ (self._min_pagesize - 1)


    def _check_for_bottom_chunks(self, chunk, heap_end):
        """Checks the current chunk for conditions normally only found on the
        second last chunk of a heap, when there are more heaps following.
        """

        if chunk.chunksize() <= self._minsize and \
                (chunk.v() + chunk.chunksize() + (self._size_sz * 2)) == \
                heap_end:
            return True

        return False


    def _allocated_chunks_for_thread_arena(self, arena):
        """Returns all allocated chunks contained in all heaps for the given
        arena, assuming the arena is not the main_arena."""

        if arena.is_main_arena:
            self.session.logging.warn(
                "Unexpected: This method has been called with the main_arena.")
            # since main_arena doesn't contain heap_infos, we return here
            return

        if arena.allocated_chunks:
            for chunk in arena.allocated_chunks:
                yield chunk

            return

        elif self._preserve_chunks:
            arena.allocated_chunks = list()

        heap_count = len(arena.heaps)

        for i in range(heap_count):
            heap = arena.heaps[i]
            current_border = heap.v() + heap.size
            hit_heap_bottom = False
            last_chunk = None
            curr_chunk = None

            for next_chunk in heap.first_chunk.next_chunk_generator():
                if not curr_chunk:
                    curr_chunk = next_chunk
                    continue

                last_chunk = curr_chunk

                if (curr_chunk.v() + curr_chunk.chunksize()) == current_border:
                    # we hit the top chunk
                    break

                else:
                    is_in_use = next_chunk.prev_inuse()

                    # on multiple heaps, for all but the last heap, the old
                    # top chunk is divided in at least two chunks at the
                    # bottom, where the second last has a size of
                    # minimum 2 * SIZE_SZ and maximum MINSIZE the last chunk
                    # has a size of 2* SIZE_SZ while the size field is set
                    # to 0x1 (the PREV_INUSE bit is set) and the prev_size
                    # contains the second last chunks size
                    # (min: 2 * SIZE_SZ , max: MINSIZE)
                    #
                    # see the part for creating a new heap within the
                    # sysmalloc function in malloc/malloc.c
                    # For glibc-2.23 beginning with line 2417
                    #
                    # as this behavior is included since version 2.0.1 from
                    # 1997, it should be safe to rely on it for most glibc
                    # versions
                    if curr_chunk.chunksize() <= self._minsize \
                            and (curr_chunk.v() + curr_chunk.chunksize()
                                 + (self._size_sz * 2)) == current_border:

                        # The last condition also tests if there are further
                        # heaps following.
                        # - if not, the current chunk which is only
                        #   size_sz * 2 bytes away from
                        #
                        # - heap border shouldn't normally exist
                        if next_chunk.chunksize() == 0 and is_in_use and \
                                (next_chunk.get_prev_size()
                                 == curr_chunk.chunksize()) and \
                                i < (heap_count - 1):
                            # we probably hit the bottom of the current heap
                            # which should'nt be the last one
                            self.session.logging.info(
                                "We hit the expected two chunks at the bottom "
                                "of a heap. This is a good sign.")
                            hit_heap_bottom = True

                            curr_chunk.is_bottom_chunk = True

                            if self._preserve_chunks:
                                arena.allocated_chunks.append(curr_chunk)

                            yield curr_chunk


                            break

                        elif curr_chunk.chunksize() < self._minsize:
                            self.session.logging.warn(
                                "Unexpected: We hit a chunk at offset 0x{0:x} "
                                "with a size smaller than the default minimum "
                                "size for a chunk but which appears to be "
                                "not part of the typical end of a heap. This "
                                "might either indicate a fatal error, or "
                                "maybe a custom libc implementation/custom "
                                "compile time flags.".format(curr_chunk.v()))

                        else:
                            self.session.logging.warn(
                                "Unexpected: We hit a chunk at offset 0x{0:x} "
                                "which presumably should have been the second "
                                "last chunk of that heap, but some conditions "
                                "don't meet.".format(curr_chunk.v()))

                        if curr_chunk not in arena.freed_fast_chunks:
                            self._check_and_report_non_main_arena(curr_chunk,
                                                                  is_in_use)

                            if self._preserve_chunks:
                                arena.allocated_chunks.append(curr_chunk)

                            yield curr_chunk

                    # normal chunk, not located at the bottom of the heap
                    else:
                        if self._check_and_report_allocated_chunk(
                                arena, curr_chunk, next_chunk, current_border):

                            self._check_and_report_non_main_arena(curr_chunk,
                                                                  is_in_use)

                            if self._preserve_chunks:
                                arena.allocated_chunks.append(curr_chunk)

                            yield curr_chunk



                curr_chunk = next_chunk

            if not hit_heap_bottom and \
                    (last_chunk.v() + last_chunk.chunksize()) < current_border:
                self.session.logging.warn(
                    "Seems like we didn't hit the top chunk or the bottom of "
                    "the current heap at offset: 0x{0:x}".format(heap.v()))


    def _allocated_chunks_for_main_arena(self):
        """Returns all allocated chunks for the main_arena's heap.
        mmap'ed regions are not included.
        """

        arena = self.get_main_arena()

        if arena.allocated_chunks:
            for chunk in arena.allocated_chunks:
                yield chunk

        else:
            current_border = 0
            if self._preserve_chunks:
                arena.allocated_chunks = list()

            if arena.first_chunk and arena.first_chunk.chunksize() > 0:
                # as the main heap can spread among multiple vm_areas, we take
                # the system_mem value as the upper boundary
                if arena.system_mem > 0:
                    current_border = arena.first_chunk.v() + arena.system_mem

                # there have been rare scenarios, in which the system_mem
                # value was 0
                else:
                    self.session.logging.warn(
                        "Unexpected: system_mem value of main arena is <= 0. "
                        "We will calculate it with the top chunk. This will "
                        "lead to follow up warnings regarding size "
                        "inconsistencies.")
                    current_border = arena.top.v() + arena.top.chunksize()

                last_chunk = None
                curr_chunk = None

                for next_chunk in arena.first_chunk.next_chunk_generator():
                    last_chunk = curr_chunk
                    if not curr_chunk:
                        curr_chunk = next_chunk
                        continue

                    if (curr_chunk.v() + curr_chunk.chunksize()) \
                            == current_border:
                        # reached top chunk
                        break

                    else:
                        if self._check_and_report_allocated_chunk(
                                arena, curr_chunk, next_chunk, current_border):

                            if self._preserve_chunks:
                                arena.allocated_chunks.append(curr_chunk)

                            yield curr_chunk


                    curr_chunk = next_chunk

                if (last_chunk.v() + last_chunk.chunksize()) < current_border:
                    self.session.logging.warn("Seems like we didn't hit the "
                                              "top chunk for main_arena.")

            elif arena.first_chunk and arena.first_chunk.chunksize() == 0:
                if not self._libc_offset:
                    self.session.logging.warn(
                        "The first main arena chunk seems to have a zero "
                        "size. As we didn't find a mapped libc module, the "
                        "reason might be a statically linked executable. "
                        "Please provide offset for the malloc_par struct "
                        "(symbol name is 'mp_'). Another reason might be "
                        "swapped memory pages.")

                else:
                    self.session.logging.warn(
                        "Unexpected error: The first main arena chunk "
                        "seems to have a zero size. The reason might be "
                        "swapped memory pages. Walking the chunks is aborted.")




    def get_all_allocated_chunks_for_arena(self, arena):
        """Returns all allocated chunks for a given arena.
        This function is basically a wrapper around
        _allocated_chunks_for_main_arena and allocated_chunks_for_thread_arena.
        """

        if not arena:
            self.session.logging.error(
                "Error: allocated_chunks_for_arena called with an empty arena")
            if self.session.GetParameter("debug"):
                pdb.post_mortem()

            return

        if arena.freed_fast_chunks is None or arena.freed_chunks is None:
            self.session.logging.error(
                "Unexpected error: freed chunks seem to not be initialized.")
            if self.session.GetParameter("debug"):
                pdb.post_mortem()

            return

        if arena.is_main_arena:
            for i in self._allocated_chunks_for_main_arena():
                yield i

        else:
            # not main_arena
            for chunk in self._allocated_chunks_for_thread_arena(arena):
                yield chunk


    # at least the function depends on getting allocated chunks first and then
    # freed chunks, so this order shouldn't be changed
    def get_all_chunks(self):
        """Returns all chunks (allocated, freed and MMAPPED chunks)."""

        for chunk in self.get_all_allocated_chunks():
            yield chunk

        for freed_chunk in self.get_all_freed_chunks():
            yield freed_chunk



    def get_all_allocated_main_chunks(self):
        """Returns all allocated chunks belonging to the main arena (excludes
        thread and MMAPPED chunks)."""

        for chunk in self.get_all_allocated_chunks_for_arena(
                self.get_main_arena()):
            yield chunk


    def get_all_allocated_thread_chunks(self):
        """Returns all allocated chunks which belong to a thread arena."""

        if self.get_main_arena():
            for arena in self.arenas:
                if not arena.is_main_arena:
                    for chunk in self.get_all_allocated_chunks_for_arena(
                            arena):
                        yield chunk


    def get_all_allocated_chunks(self):
        """Returns all allocated chunks, no matter to what arena they belong
        or if they are MMAPPED or not."""

        if self.get_main_arena():
            for arena in self.arenas:
                for chunk in self.get_all_allocated_chunks_for_arena(arena):
                    yield chunk

        for chunk in self.get_all_mmapped_chunks():
            yield chunk



    def get_all_freed_fastbin_chunks(self):
        """Returns all freed fastbin chunks, no matter to what arena they
        belong."""

        if self.get_main_arena():
            for arena in self.arenas:
                for free_chunk in arena.freed_fast_chunks:
                    yield free_chunk


    def get_all_freed_bin_chunks(self):
        """Returns all freed chunks, no matter to what arena they belong."""

        if self.get_main_arena():
            for arena in self.arenas:
                for free_chunk in arena.freed_chunks:
                    yield free_chunk


    def get_all_freed_chunks(self):
        """Returns all top chunks, freed chunks and freed fastbin chunks,
        no matter to what arena they belong."""

        if self.get_main_arena():
            for freed_chunk in self.get_all_freed_fastbin_chunks():
                yield freed_chunk

            for freed_chunk in self.get_all_freed_bin_chunks():
                yield freed_chunk

            for arena in self.arenas:
                if arena.top_chunk:
                    yield arena.top_chunk


    def _last_heap_for_vma(self, vma):
        """Returns the last heap_info within the given vma."""

        heap_hit = None

        if self.get_main_arena:
            for arena in self.arenas:
                for heap in arena.heaps:
                    if vma.vm_start <= heap.v() < vma.vm_end:
                        if not heap_hit or heap.v() > heap_hit.v():
                            heap_hit = heap

        return heap_hit




    def heap_for_ptr(self, ptr):
        """Returns the heap from the internal heap lists, the given pointer
        belongs to."""

        if self.get_main_arena:
            ptr_offset = None

            if isinstance(ptr, Number):
                ptr_offset = ptr

            else:
                ptr_offset = ptr.v()

            for arena in self.arenas:
                for heap in arena.heaps:
                    if heap.v() <= ptr_offset < (heap.v() + heap.size):
                        return heap

        return None


    # We don't use the code from glibc for this function, as it depends on the
    # HEAP_MAX_SIZE value and we might not have the correct value
    def _heap_for_ptr(self, ptr, vma=None, suppress_warning=False):
        """Returns a new heap_info struct object within the memory region, the
        given pointer belongs to. If the vm_area contains multiple heaps it
        walks all heap_info structs until it finds the corresponding one.
        """

        if self._libc_profile_success:
            ptr_offset = None

            if isinstance(ptr, Number):
                ptr_offset = ptr

            else:
                ptr_offset = ptr.v()

            if not vma:
                vma = get_vma_for_offset(self.vmas, ptr_offset)['vma']

            if vma:
                heap_info = self.profile._heap_info(offset=vma.vm_start,
                                                    vm=self.process_as)

                # there might be at least two heaps in one vm_area
                while heap_info.v() + heap_info.size < ptr_offset:
                    heap_info = self.profile._heap_info(
                        offset=heap_info.v() + heap_info.size,
                        vm=self.process_as)

                if heap_info.ar_ptr not in self.arenas and not \
                        suppress_warning:
                    self.session.logging.warn(
                        "The arena pointer of the heap_info struct gathered "
                        "from the given offset {0:x} does not seem to point "
                        "to any known arena. This either indicates a fatal "
                        "error which probably leads to unreliable results "
                        "or might be the result from using a pointer to a "
                        "MMAPPED region.".format(ptr_offset)
                    )

                return heap_info

            else:
                self.session.logging.warn(
                    "No vm_area found for the given pointer 0x{:x}."
                    .format(ptr_offset))
        else:
            self.session.logging.error(
                "Libc profile is not loaded, hence no struct or constant "
                "information. Aborting")

        return None

    def _get_number_of_cores(self):
        """Returns the number of cpu cores for the current memory image."""

        return len(list(cpuinfo.CpuInfo(session=self.session).online_cpus()))


    def _get_max_number_of_arenas(self):
        """Returns the maximum number of supported arenas. This value depends
        on the number of cpu cores."""

        cores = self._get_number_of_cores()
        return cores * (2 if self._size_sz == 4 else 8)


    def _check_arenas(self, arena, deactivate_swap_check=False):
        """Iterates the next field of the malloc_state struct and checks if we
        end up at the same malloc_state after the maximum number of arenas for
        the current system. Checks also for arena structs being part of
        swapped memory pages."""

        # This function is only reliable, if we have the offset to mp_
        if not self.mp_:
            # at least we test arena for being swapped
            if not deactivate_swap_check:
                self._check_and_report_arena_for_being_swapped(arena)

            return None

        # max arena value can be adjusted at runtime via mallopt func:
        # see malloc/malloc.c line 4753 and
        # http://man7.org/linux/man-pages/man3/mallopt.3.html
        #
        # or on startup via env vars (see also link)
        # if not, this member is 0
        arena_max = self.mp_.arena_max
        if arena_max > 0x100:
            self.session.logging.warn(
                "The maximum number of arenas, gathered from the malloc_par "
                "struct is unexpected high ({:d}). The reason might be a "
                "wrong mp_ offset and will in this case, most probably, lead "
                "to follow up errors.".format(arena_max))

        if arena_max == 0:
            # The maximum number of arenas is calculated with the macro
            # 'NARENAS_FROM_NCORES' - See malloc/arena.c
            arena_max = self._get_max_number_of_arenas()
            cores = self._get_number_of_cores()

            # In the case of one core, there can be one more arena than
            # the result from 'NARENAS_FROM_NCORES'
            # See function 'arena_get2' in malloc/arena.c
            if cores == 1:
                arena_max += 1

        if arena_max == 0:
            self.session.logging.warn(
                "The result for arena_max has been 0. This shouldn't be "
                "the case and has to be looked into.")

            if not deactivate_swap_check:
                # as the following for loop will in this case not loop over
                # any arena, we check the current arena at least for being
                # swapped
                self._check_and_report_arena_for_being_swapped(arena)

        curr_arena = arena
        for _ in range(arena_max):
            swap_check_result = self._check_and_report_arena_for_being_swapped(
                curr_arena) if not deactivate_swap_check else None

            if swap_check_result is not True:
                curr_arena = curr_arena.next
                if arena == curr_arena:
                    return True

            else:
                break

        return False



    def __init__(self, **kwargs):
        super(HeapAnalysis, self).__init__(**kwargs)

        self._libc_profile_success = False
        self._libc_offset = None
        self.arenas = []
        self.process_as = None

        # all vmas belonging to the current task
        self.vmas = None

        # only the vmas that we consider to belong to the current task's heap
        self.heap_vmas = None

        self._size_sz = None
        self._malloc_alignment = None
        self._malloc_align_mask = None
        self._minsize = None

        self.mp_ = None
        self.mp_offset = self.plugin_args.malloc_par
        self._mmapped_warnings = set()
        self._is_statically_linked = False
        self._first_chunk_distance = 0

        self.task = None
        self.statistics = None
        self._mmap_slack_space = dict()
        self._heap_slack_space = dict()
        self._hidden_chunks = set()
        self._stack_vmas_and_offsets = None

        self._preserve_chunks = False

        self._min_pagesize = 4096

        if self.session.profile.metadata("arch") == 'I386':
            self._size_sz = 4

        elif self.session.profile.metadata("arch") == 'AMD64':
            self._size_sz = 8

        self._initialize_malloc_alignment()

        self._has_dummy_arena = False


    def _initialize_malloc_alignment(self, malloc_alignment=None):
        """This function initializes variables that are in relation to
        MALLOC_ALIGNMENT."""

        # if not given as argument, we first try to load it from the profile
        if not malloc_alignment:
            malloc_alignment = self.profile.get_constant('MALLOC_ALIGNMENT')

        ##### taken from malloc/malloc.c (glibc-2.23)
        # depending on glibc comment, malloc_alignment differs only on
        # powerpc32 from 2*SIZE_SZ
        self._malloc_alignment = malloc_alignment if malloc_alignment \
            else self._size_sz * 2
        self._malloc_align_mask = self._malloc_alignment - 1

        # MIN_LARGE_SIZE defines at which size the fd/bk_nextsize pointers
        # are used
        nsmallbins = self.profile.get_constant('NSMALLBINS')
        if not nsmallbins:
            nsmallbins = 64

        smallbin_width = self._malloc_alignment
        smallbin_correction = 1 if self._malloc_alignment > 2 * self._size_sz \
            else 0

        global _MIN_LARGE_SIZE
        _MIN_LARGE_SIZE = ((nsmallbins - smallbin_correction) * smallbin_width)

        #############################################


    # Goes to the top chunk of a given arena, gets its heap_info offset and
    # follows all _heap_info.prev members until the last one (for the last
    # _heap_info, the prev field is 0x0
    def _heaps_for_arena(self, arena):
        """Returns a sorted list of all heap_info structs for a given arena:
        [0] = first heap_info.
        This method is normally only called on initialization for a new task
        and further access to heaps is done via the heaps attribute of each
        arena."""

        heap_infos = list()

        if arena.top_chunk:
            last_heap_info = self._heap_for_ptr(arena.top_chunk)

            if not last_heap_info.ar_ptr.dereference() == arena:
                self.session.logging.error(
                    "Unexpected error: current heap_info's arena pointer "
                    "doesn't point to the expected arena. Maybe wrong "
                    "profile or different cause.")

            heap_infos = list(last_heap_info.walk_list('prev'))[::-1]

        return heap_infos


    def get_main_arena(self):
        """Returns the main_arena for the current task, which is the first
        arena in the arenas list. If the current instance is not intialized,
        it logs a warning."""

        if self.arenas:
            if self.arenas[0].is_main_arena:
                return self.arenas[0]

            else:
                self.session.logging.warn(
                    "First arena in the arenas list doesn't seem to be the "
                    "main_arena.")

        else:
            self.session.logging.warn(
                "There are no arenas. Maybe this instance has not been "
                "initialized for the current task. Try to initialize it via "
                "'init_for_task'.")

        return None


    def _initialize_arenas(self, main_arena):
        """Gathers all arenas, their heaps and sets main_arenas first chunk."""

        main_arena.is_main_arena = True

        for arena in main_arena.walk_list('next'):
            self.arenas.append(arena)

            if arena.is_main_arena:
                main_arena.mmapped_first_chunks = list()
                main_arena_range = get_mem_range_for_regex(
                    self.vmas, re.escape(self._main_heap_identifier))

                if main_arena_range:
                    main_arena.first_chunk = self.profile.malloc_chunk(
                        main_arena_range[0] + self._first_chunk_distance,
                        vm=self.process_as)

                else:
                    self.session.logging.warn(
                        "The current process {:d} doesn't seem to have a main "
                        "heap. There are multiple possible explanations for "
                        "that: 1. The program uses another heap implementation"
                        " (e.g. Mozilla products). 2. The process didn't touch"
                        " the heap at all (didn't allocate any chunks within "
                        "the main thread). 3. We were unable to correctly "
                        "identify the main heap. One verification possibility "
                        "is to check with the 'maps' plugin, whether or not "
                        "this process seems to have a heap."
                        .format(self.task.pid))

            else:
                arena.heaps = self._heaps_for_arena(arena)
                # in this implementation, thread arenas don't use the
                # first_chunk member, but their heaps keep them


    def _initialize_dummy_main_arena(self):
        """Creates a dummy arena, initializes relevant variables and manually
        walks the main heap vma and adds all chunks to the allocated and freed
        chunks lists."""

        self._has_dummy_arena = True
        dummy_arena = self.profile.malloc_state()

        self._initialize_arenas(dummy_arena)

        main_arena_range = get_mem_range_for_regex(
            self.vmas, re.escape(self._main_heap_identifier))

        # There might be scenarios in which there is no main heap but only
        # mmapped chunks. In this case, main_arena_range is None.
        if main_arena_range:
            dummy_arena.system_mem = main_arena_range[1] - main_arena_range[0]

            # we activate chunk preservation (if not prevented via cmdline
            # option), as we have to walk all chunks at this point anyways
            self.activate_chunk_preservation()

            if self._preserve_chunks:
                dummy_arena.allocated_chunks = list()

            curr_chunk = None
            # while there will be no freed chunk to gather, we still test for
            # it as we need to walk the chunks anyways to get to the top chunk
            for next_chunk in dummy_arena.first_chunk.next_chunk_generator():
                if not curr_chunk:
                    curr_chunk = next_chunk
                    self._check_and_report_chunksize(curr_chunk,
                                                     main_arena_range[1])
                    continue

                if (curr_chunk.v() + curr_chunk.chunksize()) \
                        == main_arena_range[1] and curr_chunk.get_size() > 0x0:
                    # we hit top chunk
                    curr_chunk.is_top_chunk = True
                    dummy_arena.top_chunk = curr_chunk

                    break

                self._check_and_report_chunksize(next_chunk,
                                                 main_arena_range[1])
                is_in_use = next_chunk.prev_inuse()

                if (curr_chunk.v() + curr_chunk.chunksize()) \
                        < main_arena_range[1] and not is_in_use:

                    curr_chunk.is_bin_chunk = True
                    dummy_arena.freed_chunks.append(curr_chunk)


                elif self._preserve_chunks:
                    dummy_arena.allocated_chunks.append(curr_chunk)

                curr_chunk = next_chunk


            if dummy_arena.top_chunk:
                end = dummy_arena.top_chunk.v() \
                    + dummy_arena.top_chunk.chunksize()
                if dummy_arena.system_mem != end - main_arena_range[0]:
                    self.session.logging.warn(
                        "Unexpected mismatch: memory range for main heap "
                        "is not equal to the range calculated with the top "
                        "chunk. This is unexpected, indicates a problem and "
                        "will most probably lead to unreliable results.")




    def _mark_heap_vm_areas(self):
        """Marks all vm_areas containing known heap_info structs with
        '_heap_vma_identifier'. This flag is required by other functions.
        The marking process is normally done automatically in the function
        _get_vmas_for_task, but in the case where no offset for the main arena
        and no main heap is present, this step fails."""

        known_heaps = [heap for arenas in self.arenas for heap in arenas.heaps]

        for heap in known_heaps:
            vma = get_vma_for_offset(self.vmas, heap.v())
            if vma:
                vma['name'] = self._heap_vma_identifier


    def _check_heap_consistency(self):
        """Searches manually for heap_info structs on every potential heap
        area memory region, which points to a known arena. If it finds one
        that is not part of the already known heaps, it prints a warning."""

        known_heaps = [heap for arenas in self.arenas for heap in arenas.heaps]
        temp_heaps = set()
        for vm_area in self.vmas:
            name = vm_area['name']
            vma = vm_area['vma']
            if name == self._heap_vma_identifier or \
                    name == self._pot_mmapped_vma_identifier:
                heap_info = self._heap_for_ptr(vma.vm_start,
                                               vma=vma,
                                               suppress_warning=True)

                if heap_info.ar_ptr in self.arenas:

                    if heap_info not in known_heaps:
                        temp_heaps.add(heap_info)

                    while heap_info.v() + heap_info.size < vma.vm_end \
                            and heap_info.ar_ptr in self.arenas:

                        heap_info = self.profile._heap_info(
                            offset=heap_info.v() + heap_info.size,
                            vm=self.process_as)

                        if heap_info.ar_ptr in self.arenas \
                                and heap_info not in known_heaps:
                            temp_heaps.add(heap_info)

        additional_heaps = set()
        for temp_heap_info in temp_heaps:
            for heap_info in temp_heap_info.walk_list('prev'):
                if heap_info not in known_heaps:
                    additional_heaps.add(heap_info)

        additional_heaps = additional_heaps.union(temp_heaps)

        if additional_heaps:
            self.session.logging.warn(
                "We probably found at least one heap, which is not part of our"
                "internal list. This shouldn't be the case, indicates a "
                "problem and will lead to unreliable results. The offset(s) "
                "of the additional heap(s) is/are: "
                + ("0x{:x} " * len(additional_heaps))
                .format(*[heap.v() for heap in additional_heaps]))


    def _check_and_correct_empty_space_in_heaps(self):
        """There are scenarios in which the last heap of an arena contains
        additional space which is not covered by the top chunk, leading to
        deviating results with the compare_vma_sizes_with_chunks function.
        This function tries to identify those areas and add their size to the
        _heap_slack_space attribute."""

        for arena in self.arenas:
            if not arena.is_main_arena:
                # there are scenarios in which one vma shares heap_infos from
                # different arenas so we gather here the last heap_info of a
                # given vma and test for slack space
                vma = get_vma_for_offset(self.vmas, arena.top_chunk.v())['vma']
                heap = self._last_heap_for_vma(vma)

                if heap.v() + heap.size < vma.vm_end:
                    self._heap_slack_space[heap] = (vma.vm_end - (heap.v()
                                                                  + heap.size))



    def _initialize_heap_first_chunks(self):
        """Gathers the first chunk for each heap and sets it as first_chunk in
        the _heap_info class."""

        heap_offset = self.profile.get_obj_size('_heap_info')
        malloc_offset = self.profile.get_obj_size('malloc_state')

        for arena in self.arenas:
            # main_arena has no associated _heap_info structs
            if arena.is_main_arena:
                continue

            for heap in arena.heaps:
                first_chunk_offset = heap.v() + heap_offset

                # only the first heap area contains also the malloc_state
                # the prev field for the first heap_info is 0x0
                if heap.prev == 0x0:
                    first_chunk_offset += malloc_offset

                # chunks are aligned, so in the case of non main_arenas, the
                # address after the heap_info (and malloc_state) is probably
                # not directly the first chunk but a few bytes after. So we
                # try to find the first non-zero size_sz bytes.
                #
                # To prevent looking in the middle of a 8 byte size from a
                # large chunk, we walk in steps of 8 bytes, as this is also
                # the minimal alignment (32 bit)
                first_chunk_offset = self.get_aligned_address(
                    first_chunk_offset, different_align_mask=7)

                expected_first_chunk_offset = self.get_aligned_address(
                    first_chunk_offset)

                for _ in range(8):
                    temp = self.process_as.read(first_chunk_offset,
                                                self._size_sz)

                    temp = struct.unpack('I' if self._size_sz == 4 else 'Q',
                                         temp)[0]

                    # the first member of the malloc_chunk is the prev_size
                    # field, which should be 0x0 for the first chunk and the
                    # following member is size which should be > 0x0.
                    if temp != 0x0:
                        first_chunk_offset -= self._size_sz
                        break

                    first_chunk_offset += self._size_sz

                # Normally, the first chunk is exactly the aligned address
                # after the structs, but if we find it somewhere else, it is
                # an indicator for another libc version (e.g. differing
                # structs) that we don't have the correct vtypes for or
                # another MALLOC_ALIGNMENT value
                if first_chunk_offset != expected_first_chunk_offset:
                    self.session.logging.warn(
                        "We identified an unexpected address deviation, which "
                        "indicates another glibc version than the one we are "
                        "using or another value for MALLOC_ALIGNMENT. Verify "
                        "which version is used and provide the debug "
                        "information for that version. At the moment, "
                        "officially only those versions are supported when "
                        "not providing debug information for a specific "
                        "version: {:s}"
                        .format(', '.join(_SUPPORTED_GLIBC_VERSIONS)))

                    if self.session.profile.metadata("arch") == 'I386':
                        self.session.logging.warn(
                            "We just try for now to adjust the "
                            "MALLOC_ALIGNMENT to 16 byte (instead of 8). This "
                            "might solve the problem.")
                        self._initialize_malloc_alignment(malloc_alignment=16)

                heap.first_chunk = self.profile.malloc_chunk(
                    offset=first_chunk_offset, vm=self.process_as)

                if arena.top_chunk != heap.first_chunk:
                    self._check_and_report_non_main_arena(
                        heap.first_chunk, heap.first_chunk.is_in_use())



    def _initialize_mmapped_first_chunks(self):
        """Gathers the first chunk for each MMAPPED region and sets it on the
        main_arena. First chunks for MMAPPED regions are only kept in the
        main_arena, which is the first arena in the 'arenas' attribute of the
        current class."""

        # we first gather all vm_area offsets belonging to the main heap or
        # thread heaps
        heap_offsets = []
        main_arena = self.get_main_arena()
        if main_arena.first_chunk:
            heap_offsets.append(main_arena.first_chunk.v())

        for arena in self.arenas:
            for heap in arena.heaps:
                heap_offsets.append(heap.v())

        # now we gather all vm_areas that do not contain a known
        # heap_info struct
        for vm_area in self.vmas:
            name = vm_area['name']
            vma = vm_area['vma']
            if (name == self._heap_vma_identifier
                    or name == self._pot_mmapped_vma_identifier) \
                    and str(vma.vm_flags).startswith('rw') \
                    and vma.vm_start not in heap_offsets:

                mmap_chunk = self.profile.malloc_chunk(offset=vma.vm_start,
                                                       vm=self.process_as)

                if self._check_and_report_mmapped_chunk(mmap_chunk, vma):
                    main_arena.mmapped_first_chunks.append(mmap_chunk)


    def _initialize_heap_vma_list(self):
        """Searches for vmas that are known to belong to the heap and adds
        them to the internal heap_vmas list."""

        self.heap_vmas = []

        for vma in self.vmas:
            if vma['name'] == self._main_heap_identifier:
                self.heap_vmas.append(vma)

        for arena in self.arenas:
            if arena.is_main_arena:
                for mmap_chunk in arena.mmapped_first_chunks:
                    vma = get_vma_for_offset(self.vmas, mmap_chunk.v())
                    if vma not in self.heap_vmas:
                        self.heap_vmas.append(vma)

            else:
                for heap in arena.heaps:
                    vma = get_vma_for_offset(self.vmas, heap.v())
                    if vma not in self.heap_vmas:
                        self.heap_vmas.append(vma)



    def _check_and_report_non_main_arena(self, chunk, chunk_in_use):
        """Checks the given chunk for the NON_MAIN_ARENA bit and prints a
        warning if not set. This functions should obviously only be used with
        chunks not belonging to main_arena but also not for MMAPPED chunks
        (they don't have the NON_MAIN_ARENA bit set)."""

        if chunk_in_use and not chunk.non_main_arena():
            self.session.logging.warn(
                "Unexpected error: The non main arena chunk at offset 0x{0:x} "
                "doesn't have the NON_MAIN_ARENA bit set.".format(chunk.v()))


    def _log_mmapped_warning_messages(self, warning):

        if not self.mp_:
            self.session.logging.warn(warning)

        else:
            self._mmapped_warnings.add(warning)


    # As there might be multiple scenarios, in which a vm_area is mistakenly
    # treated as a mmapped region (see following warn messages for details),
    # we strictly test for prev_size to be 0x0 (normally always the case for
    # the first chunk in a memory region), the size to be != 0 and the mmapped
    # bit to be set
    def _check_and_report_mmapped_chunk(self, mmap_chunk, mmap_vma):
        """Checks the given chunk for various MMAPPED chunk specific
        attributes. Depending on the results and the location of the chunk,
        a info or warning is printed."""

        base_string = ("Current MMAPPED chunk at offset 0x{0:x} "
                       .format(mmap_chunk.v()))

        zero_first_chunk_error_reasons = (
            "As this chunk resides at the beginning of the vm_area, "
            "this fact might have multiple reasons: "
            "1. It is part of a MMAPPED region but there are not yet any "
            "allocated chunks. 2. The current vm_area is in fact the rest of "
            "a dead thread stack or belongs  to a mapped file, which is not "
            "disginguishable from heap-vmas at the moment. "
            "3. There might be an unexpected error. "
            "In the first two cases, this warning can be considered harmless.")

        zero_middle_chunk_error_reasons = (
            "In the current case, this fact might have the following reasons: "
            "1. It is the result from an MMAPPED region, which doesn't use "
            "the whole space for its chunks (in this case harmless). "
            "2. The current data belongs to an MMAPPED region, which shares "
            "its vm_area with an mapped file or other data (also harmless). "
            "3. It results from an accidently chosen vm_area to be part of "
            "the heap (more specifically, to be an MMAPPED chunks region). "
            "This can happen with old thread stacks or vm_areas of mapped "
            "file and indicates an error and leads to wrong results. "
            "4. An unexpected error (might lead to unrealiable results).")

        first_chunk_error_reasons = (
            "As this chunk resides at the beginning of the vm_area, "
            "this fact might have the following reasons: "
            "1. The current vm_area is in fact the rest of a dead thread "
            "stack or belongs to a mapped file, which is not disginguishable "
            "from heap-vmas at the moment. "
            "2. There might be an unexpected error. "
            "In the first case, this warning can be considered harmless.")

        middle_chunk_error_reasons = (
            "In the current case, this fact might have the following reasons: "
            "1. The current data belongs to an MMAPPED region, which shares "
            "its vm_area with an mapped file or other data (in this case "
            "harmless). 2. It results from an accidently chosen vm_area to be "
            "part of the heap (more specifically, to be an MMAPPED chunks "
            "region). This can happen with old thread stacks or vm_areas of "
            "mapped file and indicates an error and leads to wrong results. "
            "3. An unexpected error (might lead to unrealiable results).")


        # as the size for mmapped chunks is at least pagesize, we expect them
        # to be >= 4096
        # see glibc_2.23 malloc/malloc.c lines 2315 - 2318
        if mmap_chunk.get_prev_size() != 0 or \
                mmap_chunk.chunksize() < self._min_pagesize or \
                mmap_chunk.chunksize() % self._min_pagesize != 0 or \
                mmap_chunk.v() + mmap_chunk.chunksize() > mmap_vma.vm_end:

            if mmap_chunk.get_prev_size() == 0 and mmap_chunk.get_size() == 0:
                base_string += "has zero size. "

                if mmap_chunk.v() == mmap_vma.vm_start:

                    # it is possible that a vm_area is marked as rw and does
                    # not contain a stack or heap or mmap region. we
                    # identified this case only when no threads are active
                    number_of_heap_vmas = 0
                    for vma in self.vmas:
                        if vma['name'] == self._heap_vma_identifier:
                            number_of_heap_vmas += 1

                    if number_of_heap_vmas <= 1 and len(self.arenas) == 1 \
                            and not self._are_there_any_threads():
                        self.session.logging.info(
                            base_string + "In this case, it seems "
                            "to be the result from a process with no threads "
                            "and a not yet used memory region, hence "
                            "indicating nothing abnormal.")

                    else:
                        self.session.logging.info(
                            base_string + zero_first_chunk_error_reasons)

                else:
                    self._log_mmapped_warning_messages(
                        base_string + zero_middle_chunk_error_reasons)
                    self._mmap_slack_space[mmap_chunk] = (mmap_vma.vm_end -
                                                          mmap_chunk.v())

            else:
                base_string += "has invalid values. "
                if mmap_chunk.v() == mmap_vma.vm_start:
                    self.session.logging.info(base_string +
                                              first_chunk_error_reasons)

                else:
                    self._log_mmapped_warning_messages(
                        base_string + middle_chunk_error_reasons)
                    self._mmap_slack_space[mmap_chunk] = (mmap_vma.vm_end -
                                                          mmap_chunk.v())


        elif mmap_chunk.prev_inuse() or mmap_chunk.non_main_arena():
            base_string += ("has either the prev_inuse or non_main_arena bit "
                            "set, which is normally not the case for MMAPPED "
                            "chunks.")

            if mmap_chunk.v() == mmap_vma.vm_start:
                self.session.logging.info(
                    base_string + first_chunk_error_reasons)

            else:
                self._log_mmapped_warning_messages(
                    base_string + middle_chunk_error_reasons)

                self._mmap_slack_space[mmap_chunk] = (mmap_vma.vm_end
                                                      - mmap_chunk.v())


        elif not mmap_chunk.is_mmapped():
            base_string += "doesn't have the is_mmapped bit set. "

            if mmap_chunk.v() == mmap_vma.vm_start:
                self.session.logging.info(
                    base_string + first_chunk_error_reasons)

            else:
                self._log_mmapped_warning_messages(
                    base_string + middle_chunk_error_reasons)

                self._mmap_slack_space[mmap_chunk] = (mmap_vma.vm_end
                                                      - mmap_chunk.v())

        elif not self._check_mmap_alignment(mmap_chunk.v()):
            self._log_mmapped_warning_messages(
                base_string + "is not aligned. As chunks are normally always "
                "aligned, this indicates a mistakenly chosen mmapped chunk "
                "and probably results in wrong results.")

        # everything is ok
        else:
            return True

        return False


    def _are_there_any_threads(self):
        """This function searches for vmas containing the stack for a thread
        and returns True if it finds at least one."""

        # mm_users holds the number of mm_struct users. when a thread is
        # created, he gets hands on the mm_struct and the counter is
        # increased: mm_users >= 2 means there are threads
        if self.task.mm.mm_users.counter >= 2:
            return True

        # if the first test fails, we still look for thread stack segments
        for vma in self.vmas:
            if vma['name'].startswith('[stack:'):
                return True

        return False


    def _get_max_fast_chunk_size(self):
        """Returns the maximum size for the data part of fast chunks.
        E.g. for 32 bit architectures, the max size is normally 64 bytes,
        but 4 bytes are used by the size field of the malloc_chunk struct,
        which leaves the data part 60 bytes."""

        return 60 if self._size_sz == 4 else 120


    def iterate_through_chunks(self, first_chunk, mem_end, only_free=False,
                               only_alloc=False):
        """This function iterates chunk after chunk until hitting mem_end.
        Tests for allocation status are not made via bins/fastbins but with
        chunk flags. Note: This function will not return the last chunk, if
        only_free or/and only_alloc is set as there is no PREV_INUSE bit which
        could be tested."""

        if not (only_free or only_alloc):
            for curr_chunk in first_chunk.next_chunk_generator():
                if (curr_chunk.v() + curr_chunk.chunksize()) < mem_end:
                    yield curr_chunk

                else:
                    yield curr_chunk
                    break


        else:
            curr_chunk = None

            for next_chunk in first_chunk.next_chunk_generator():
                if not curr_chunk:
                    curr_chunk = next_chunk
                    continue


                if (curr_chunk.v() + curr_chunk.chunksize()) < mem_end:
                    is_in_use = next_chunk.prev_inuse()

                    if only_free and not is_in_use or \
                            only_alloc and is_in_use:
                        yield curr_chunk

                else:
                    # we hit last/top chunk. as there is no following chunk, we
                    # can't examine the PREV_INUSE bit
                    break

                curr_chunk = next_chunk



    def _offset_in_heap_range(self, offset):
        """Returns true if the given offset resides in a vma potentially
        belonging to the heap. This function is only used while carving for
        the main arena and hence can not use the later on generated internal
        heap_vmas list."""

        for vma in self.vmas:
            if vma['vma'].vm_start <= offset < vma['vma'].vm_end:
                name = vma['name']
                if name == self._main_heap_identifier \
                        or name == self._heap_vma_identifier:
                    return True

        return False



    def _carve_main_arena(self):
        """Calling this method assumes that we don't have debug information (in
        the sense of constant offsets for data structures) for the target libc
        implementation and do not know the location of the main_arena. If the
        current task contains threads however, we are able to get the location
        of the main_arena. If there are no threads, we still are able to locate
        the main_arena by folowing the fd/bk pointers in freed chunks.
        The last attempt is done by walking the chunks of the main heap until
        the top chunk is hit. As the main arena keeps a pointer to this chunk,
        we simply search all memory regions for pointers.
        This method returns either the main_arena or None."""

        if not self._libc_profile_success:
            self.session.logging.error("No libc profile with rudimentary "
                                       "struct information available.")

            return None


        libc_range = get_libc_range(self.vmas)

        if self._are_there_any_threads():
            self.session.logging.info(
                "As there are threads, we try to gather the main_arena "
                "via the _heap_info structs.")

        else:
            self.session.logging.info(
                "We first try to gather the main_arena via died thread "
                "heaps, assuming there are any.")

        good_arenas = []
        # bad arenas don't loop with their next pointer within the maximum
        # number of arenas for the current number of cores and the architecture
        # see _check_arenas
        bad_arenas = []

        # first we try to find a heap_info struct whose ar_ptr points right
        # after itself this is the case for the first vm_area containing the
        # first heap_info and the according malloc_state struct
        for vma in self.vmas:
            if vma['name'] == self._heap_vma_identifier \
                    or vma['name'] == self._pot_mmapped_vma_identifier:

                heap_info = self.profile._heap_info(offset=vma['vma'].vm_start,
                                                    vm=self.process_as)

                # we try to find a heap_info struct which is followed by a
                # malloc_state. The prev member of the first _heap_info struct
                # (which is the one followed by the malloc_state struct) is 0x0
                heap_info_size = self.profile.get_obj_size('_heap_info')

                if vma['vma'].vm_start <= heap_info.ar_ptr.v() \
                        <= vma['vma'].vm_end:
                    heap_info_address = self.get_aligned_address(
                        heap_info_size + vma['vma'].vm_start)

                    if heap_info.ar_ptr.v() == heap_info_address \
                            and heap_info.prev.v() == 0x0:

                        arena = heap_info.ar_ptr
                        arena_consistency = self._check_arenas(
                            arena, deactivate_swap_check=True)

                        if arena_consistency is True or arena_consistency \
                                is None:
                            good_arenas.append(arena)

                        else:
                            bad_arenas.append(arena)


        reached_bad_arenas = False

        # now we try to use the potential arenas to find the main_arena
        # located in the libc
        for arena_list in good_arenas, bad_arenas:
            for arena in arena_list:
                for pot_main_arena in arena.walk_list('next'):
                    if libc_range and libc_range[0] <= pot_main_arena.v() \
                            <= libc_range[1] or not libc_range and \
                            not self._offset_in_heap_range(pot_main_arena.v()):

                        if reached_bad_arenas:
                            self.session.logging.warn(
                                "The arena pointers for the gathered "
                                "main_arena don't seem to loop. The reason "
                                "might be wrong arena pointers and probably "
                                "leads to unreliable results.")

                        else:
                            self.session.logging.info(
                                "We most probably found the main_arena via "
                                "heap_info structs")

                        return pot_main_arena

            reached_bad_arenas = True

        self.session.logging.info(
            "It doesn't seem like the task with pid {0:d} has any threads, "
            "and as we don't have have the main arena offset, we now try to "
            "find freed chunks and with them the location of the main_arena."
            .format(self.task.pid))


        # the previous method didn't work so we now try to gather the main
        # arena via freed chunks
        main_heap_range = get_mem_range_for_regex(
            self.vmas, re.escape(self._main_heap_identifier))

        if not main_heap_range:
            return None

        first_chunk = self.profile.malloc_chunk(
            offset=main_heap_range[0] + self._first_chunk_distance,
            vm=self.process_as)

        offset_to_top = self.profile.get_obj_offset("malloc_state", "top")


        # not used right here, but part of the next method of carving the
        # main arena
        last_freed_chunk = None

        for free_chunk in self.iterate_through_chunks(first_chunk,
                                                      main_heap_range[1],
                                                      only_free=True):

            last_freed_chunk = free_chunk

            # we now try to follow the bk links to get to the main_arena
            for curr_free_chunk in free_chunk.walk_list('bk'):

                if libc_range and libc_range[0] <= curr_free_chunk.v() \
                        <= libc_range[1] or not libc_range and \
                        not self._offset_in_heap_range(curr_free_chunk.v()):
                    # we are now within the main_arena and try
                    # to find the top chunk by going backwards

                    offset_to_binmap = self.profile.get_obj_offset(
                        "malloc_state", "binmap")
                    maximum_offset_to_top = offset_to_binmap - offset_to_top

                    curr_off = curr_free_chunk.v()
                    fmt = 'I' if self._size_sz == 4 else 'Q'

                    # as between the bins and top are only pointers, walking in
                    # size_sz steps should be no problem
                    for i in range(0, maximum_offset_to_top, self._size_sz):
                        temp = self.process_as.read(curr_off - i,
                                                    self._size_sz)
                        temp = struct.unpack(fmt, temp)[0]

                        if main_heap_range[0] <= temp <= main_heap_range[1]:
                            pot_top = self.profile.malloc_chunk(
                                offset=temp, vm=self.process_as)

                            if pot_top.v() + pot_top.chunksize() == \
                                    main_heap_range[1]:
                                # we hit top chunk

                                self.session.logging.info(
                                    "We found the main_arena via a freed "
                                    "chunk.")

                                return self.profile.malloc_state(
                                    offset=(curr_off - i) - offset_to_top,
                                    vm=self.process_as)


        # Ending up here means all previous methods were not able to find the
        # main arena. The last method we try at this point is to search for
        # pointers to the top chunk. At least the main_arena should have a
        # pointer to the top chunk
        #
        # TODO the way we do this (including the last method) is inefficient,
        # as for most cases, all chunks from the main heap are walked twice
        #  => improve it!

        # we walk from the last freed chunk from the previous method or from
        # the first chunk until the top chunk
        if last_freed_chunk:
            first_chunk = last_freed_chunk

        top_chunk = None
        for curr_chunk in self.iterate_through_chunks(first_chunk,
                                                      main_heap_range[1]):
            top_chunk = curr_chunk

        if top_chunk.v() + top_chunk.chunksize() == main_heap_range[1]:

            # we most probably found our top chunk and now search for pointers
            # to it
            for hit in self.search_vmas_for_needle(pointers=[top_chunk.v()]):
                pot_main_arena = self.profile.malloc_state(
                    offset=hit['hit'] - offset_to_top, vm=self.process_as)

                if top_chunk == pot_main_arena.top and \
                        pot_main_arena.system_mem == \
                        (top_chunk.v() + top_chunk.chunksize()
                         - main_heap_range[0]):

                    # as the 'thread arena carving' method didn't find an
                    # arena, the 'next' field should point to itself
                    if pot_main_arena.next == pot_main_arena:
                        self.session.logging.info(
                            "We found the main_arena via top chunk.")
                        return pot_main_arena

                    else:
                        arena_consistency = self._check_arenas(
                            pot_main_arena, deactivate_swap_check=True)
                        if arena_consistency is True or arena_consistency \
                                is None:
                            self.session.logging.info(
                                "We found the main_arena via top chunk.")
                            return pot_main_arena


        # This will most probably only happen, if the page containing the main
        # arena has been swapped
        self.session.logging.warn(
            "We were not able to find the main arena for task {0:d} and since "
            "we have no debug information about its offset, we can't retrieve "
            "it directly.".format(self.task.pid))

        return None


    def _reset(self):
        """Prepares the HeapAnalysis instance to work with a new process."""

        self._libc_profile_success = False
        self._libc_offset = None
        self.process_as = None
        self.arenas = []
        self.vmas = None
        self.heap_vmas = None
        self.mp_ = None
        self._mmapped_warnings = set()
        self.task = None
        self.statistics = None
        self._mmap_slack_space = dict()
        self._heap_slack_space = dict()
        self._hidden_chunks = set()
        self._stack_vmas_and_offsets = None
        self._is_statically_linked = False
        self._has_dummy_arena = False
        self._first_chunk_distance = 0


    # TODO reliable verification via page table information
    def _check_and_report_arena_for_being_swapped(self, arena):
        """Tests the fields of an arena for null bytes. If those fields are
        null, it is a good indication that the corresponding memory region has
        been swapped."""

        if arena:
            if arena.top.v() == arena.next.v() == arena.system_mem.v() == 0:
                # arena has likely been swappped
                self.session.logging.warn(
                    "Some crucial fields of the arena at offset 0x{:x} are "
                    "all null. The reason might be a wrong offset to the "
                    "main arena, a statically linked binary, a fundamental "
                    "error in this plugin, or (in most cases) swapped memory "
                    "pages. Either way, the results will most probably be "
                    "incorrect and incomplete.".format(arena.v()))
                return True

        return False


    # TODO reliable verification via page table information
    def _check_and_report_mp_for_being_swapped(self, malloc_par_struct):
        """Tests the fields of the malloc_par struct. If those fields are null,
        it is a good indication that the corresponding memory region has been
        swapped."""

        if malloc_par_struct:
            if malloc_par_struct.mmap_threshold.v() == 0:
                # memory page belonging to malloc_par struct has likely
                # been swappped
                self.session.logging.warn(
                    "At least the mmap_threshold field of the malloc_par "
                    "struct at offset 0x{:x} is null. The reason might be a "
                    "wrong offset to the malloc_par struct, a statically "
                    "linked binary, a fundamental error in this plugin, or "
                    "(in most cases) swapped memory pages. Either way, the "
                    "MMAPPED chunk algorithms will not work perfectly and "
                    "hence, some chunks might be missing."
                    .format(malloc_par_struct.v()))
                return True

        return False


    def init_for_task(self, task):
        """initializes the process address space and malloc_par struct and
        calls initialize_*. Should be the first method to be called for each
        task.
        Returns True if everything seems to be gone fine."""

        self._reset()

        # processes normally have an associated mm_struct/memory descriptor
        # if there is none, it is probably a kernel thread
        if task.mm:
            self.session.plugins.cc().SwitchProcessContext(task)

            self.task = task
            self.vmas = self._get_vmas_for_task(task)

            if self.vmas:
                self._load_libc_profile()

                if self._libc_profile_success:

                    ###### taken from malloc/malloc.c (glibc-2.23)
                    min_chunk_size = self.profile.get_obj_offset(
                        "malloc_chunk", "fd_nextsize")
                    self._minsize = self.get_aligned_address(min_chunk_size)
                    ######

                    self.process_as = task.get_process_address_space()

                    libc_range = get_libc_range(self.vmas)
                    # we prepone setting the self._libc_offset as it is
                    # required for _initialize_malloc_par
                    if libc_range:
                        self._libc_offset = libc_range[0]

                    self._initialize_malloc_par()

                    if not libc_range:
                        # seems like a statically linked executable
                        self.session.logging.warn(
                            "Didn't find the libc filename in the vm_areas of "
                            "the current process: {:d} - {:s} . This might "
                            "lead to unreliable results or might be because "
                            "the executable has been statically linked."
                            .format(task.pid, repr(task.comm.v())))

                        if self.mp_:
                            # the beginning of the chunk area is pointed to by
                            # mp_.sbrk_base
                            self._is_statically_linked = True
                            main_arena_range = get_mem_range_for_regex(
                                self.vmas,
                                re.escape(self._main_heap_identifier))

                            self._first_chunk_distance = \
                                self.mp_.sbrk_base.v() - main_arena_range[0]

                    else:
                        self.session.logging.info(
                            "Found libc offset at: " + hex(self._libc_offset))

                    pot_main_arena = None

                    if self.plugin_args.main_arena:
                        main_arena_offset = self.plugin_args.main_arena
                    else:
                        main_arena_offset = self.profile.get_constant(
                            'main_arena')

                    if main_arena_offset:
                        if self._libc_offset:
                            main_arena_offset += self._libc_offset

                        pot_main_arena = self.profile.malloc_state(
                            offset=(main_arena_offset), profile=self.profile,
                            vm=self.process_as)

                    else:
                        self.session.logging.info(
                            "As it seems like we don't have debug information "
                            "for the main arena, we now try to retrieve the "
                            "main_arena via some different techniques for pid "
                            "{:d}.".format(self.task.pid))
                        pot_main_arena = self._carve_main_arena()

                    if pot_main_arena:
                        if self._check_arenas(pot_main_arena) is False:
                            self.session.logging.warn(
                                "Arena pointers don't seem to loop within the "
                                "expected range. Maybe the main_arena pointer "
                                "is wrong. This might lead to unreliable "
                                "results.")

                        # despite potential problems, we try to proceed
                        self._initialize_arenas(pot_main_arena)

                        self._mark_heap_vm_areas()

                        self._check_heap_consistency()

                        self._initialize_heap_first_chunks()
                        self._check_and_correct_empty_space_in_heaps()

                    else:
                        # no main_arena could be found, so we simply walk
                        # the main_heap for chunks
                        self.session.logging.warn(
                            "No main_arena could be found, so we simply try to"
                            " walk the chunks in the main heap. Without the "
                            "arena, fastbin chunks can't be recognized "
                            "reliably, and hence are treated as allocated "
                            "chunks. This is especially a problem on further "
                            "analysis (e.g. dumping their content).")

                        self._initialize_dummy_main_arena()

                    self._initialize_mmapped_first_chunks()
                    self._initialize_heap_vma_list()

                    self.activate_chunk_preservation()
                    self.check_and_report_size_inconsistencies()

                    return True

                else:
                    self.session.logging.error(
                        "Libc profile is not loaded, "
                        "hence no struct or constant information. Aborting")

            else:
                self.session.logging.warn(
                    "No vm_areas could be extracted from current task (maybe "
                    "kernel thread): {:s} (PID: {:d})"
                    .format(repr(task.comm.v()), task.pid))

        else:
            self.session.logging.warn(
                "Current task seems to be a kernel thread. Skipping Task: "
                "{:s} (PID: {:d})".format(repr(task.comm.v()), task.pid))

        self._reset()

        return False


    def _walk_hidden_mmapped_chunks(self, hidden_chunk):
        """Helper function for carve_and_register_hidden_mmapped_chunks.
        Walks MMAPPED chunks beginning with hidden_chunks and registers them.
        """
        new_mmapped_chunks = []

        # verification steps are triggered
        # in allocated_chunks_for_mmapped_chunk
        if hidden_chunk:
            if hidden_chunk not in self.get_main_arena().mmapped_first_chunks:
                for mmapped_chunk in self._allocated_chunks_for_mmapped_chunk(
                        hidden_chunk):
                    new_mmapped_chunks.append(mmapped_chunk)

        return new_mmapped_chunks



    def _carve_register_mmapped_chunks_hidden_behind_stack(self):
        """Tries to find hidden MMAPPED chunks behind stack segemts."""

        # list of new mmapped chunks lists (first and following chunks)
        new_mmapped_chunks = []
        relevant_vmas = []

        for vma in self.vmas:
            if not re.search('^\[stack', vma['name']):
                continue

            current_chunks = []
            last_ebp = self._ebp_unrolling(vma['ebp'], vma['vma'])
            search_start = last_ebp if last_ebp else vma['vma'].vm_start

            temp_chunk = self._search_first_hidden_mmapped_chunk(search_start,
                                                                 vma['vma'])
            current_chunks = self._walk_hidden_mmapped_chunks(temp_chunk)

            if current_chunks:
                new_mmapped_chunks.append(current_chunks)
                relevant_vmas.append([vma, current_chunks[0].v()])

            else:
                relevant_vmas.append([vma, vma['vma'].vm_end])

        self._register_hidden_mmapped_chunks(new_mmapped_chunks)
        self._stack_vmas_and_offsets = relevant_vmas


    def _search_stacks_for_mmap_pointers(self):
        """Helper function for carving hidden MMAPPED chunks.
        Searches the stack frames for pointers to identified hidden MMAPPED
        chunks and reports the findings. This function is supposed to be
        called, when the identifed MMAPPED chunk values (number and size) do
        not correspond with the malloc_par values."""

        mmapped_chunks = self.get_all_mmapped_chunks()

        if self._stack_vmas_and_offsets and mmapped_chunks:
            mmap_pointers = []
            chunk_data_offset = self.profile.get_obj_offset("malloc_chunk",
                                                            "fd")
            mmap_pointers += [x.v() + chunk_data_offset
                              for x in mmapped_chunks]

            found_pointers = set()

            for hit in self.search_vmas_for_needle(
                    pointers=mmap_pointers,
                    hidden_mmap_vmas=self._stack_vmas_and_offsets):

                found_pointers.add(hit['needle'])

            if len(found_pointers) == len(mmap_pointers):
                self.session.logging.warn(
                    "It was possible to find at least one pointer on the "
                    "stack for each of the {:d} identified MMAPPED chunks. "
                    "This is a good sign for the gathered chunks, but "
                    "probably means that there is at least one chunk missing."
                    .format(len(mmap_pointers)))
            else:
                self.session.logging.warn(
                    "Found {:d} pointer(s) to MMAPPED chunks in stack "
                    "segments out of {:d} identified MMAPPED chunks. Each "
                    "identified \"MMAPPED chunk\" with no associated pointer "
                    "on the stack might have been mistakenly chosen."
                    .format(len(found_pointers), len(mmap_pointers)))



    def _register_hidden_mmapped_chunks(self, new_mmapped_chunks):
        """Helper function for carving hidden MMAPPED chunks.
        Registers the given hidden MMAPPED chunks internally.
        """

        if new_mmapped_chunks:

            main_arena = self.get_main_arena()
            for chunks in new_mmapped_chunks:
                for chunk in chunks:
                    if chunk not in main_arena.allocated_mmapped_chunks:
                        main_arena.allocated_mmapped_chunks.append(chunk)

                    main_arena.mmapped_first_chunks.append(chunks[0])
                    self._hidden_chunks.add(chunk)


    def _carve_and_register_hidden_mmapped_chunks_globally(self):
        """Tries to find hidden MMAPPED chunks in anonymous vmas."""

        # list of new mmapped chunks lists (first and following chunks)
        new_mmapped_chunks = []

        for vma in self.vmas:
            # we walk only over anonymous and stack related vmas (for the case,
            # the ebp_unrolling went wrong)
            if vma['name'] != self._pot_mmapped_vma_identifier \
                    and vma['name'] != self._heap_vma_identifier \
                    and not re.search('^\[stack', vma['name']):
                continue

            temp_chunk = self._search_first_hidden_mmapped_chunk(
                vma['vma'].vm_start, vma['vma'])
            current_chunks = self._walk_hidden_mmapped_chunks(temp_chunk)

            if current_chunks:
                new_mmapped_chunks.append(current_chunks)

        self._register_hidden_mmapped_chunks(new_mmapped_chunks)



    def search_vmas_for_needle(self, search_string=None, search_regex=None,
                               pointers=None, vmas=None, hidden_mmap_vmas=None,
                               vma_regex=None):
        """Searches all vmas or only the given ones for the given pointer(s).
        pointers = a list of int pointers
        regex = a regex identifying relevant vm_areas
        Returns a list of hits
        """

        if search_string:
            scanner = scan.BaseScanner(profile=self.profile,
                                       session=self.session,
                                       address_space=self.process_as,
                                       checks=[('StringCheck',
                                                dict(needle=search_string))])

        elif search_regex:
            scanner = scan.BaseScanner(profile=self.profile,
                                       session=self.session,
                                       address_space=self.process_as,
                                       checks=[('RegexCheck',
                                                dict(regex=search_regex))])

        elif pointers:
            scanner = scan.PointerScanner(profile=self.profile,
                                          session=self.session,
                                          address_space=self.process_as,
                                          pointers=pointers)

        else:
            return None

        if not vmas or hidden_mmap_vmas:
            vmas = self.vmas

        if hidden_mmap_vmas:
            vmas = hidden_mmap_vmas

        result = []

        for vma in vmas:
            if vma_regex and not hidden_mmap_vmas:
                if not re.search(vma_regex, vma['name']):
                    continue

            start = vma[0]['vma'].vm_start if hidden_mmap_vmas \
                else vma['vma'].vm_start

            end = vma[1] if hidden_mmap_vmas else vma['vma'].vm_end
            length = end - start

            for hit in scanner.scan(offset=start, maxlen=length):
                temp = dict()
                temp['vma'] = vma[0] if hidden_mmap_vmas else vma
                temp['hit'] = hit
                if pointers:
                    pointer = self.process_as.read(hit, self._size_sz)
                    pointer = struct.unpack('I' if self._size_sz == 4
                                            else 'Q', pointer)[0]
                    temp['needle'] = pointer

                elif search_string:
                    temp['needle'] = search_string

                elif search_regex:
                    temp['needle'] = search_regex

                result.append(temp)

        return result


    def get_chunks_for_addresses(self, addresses, ignore_prevsize=False):
        """Returns the chunks located at the given addresses.
        The address can be at the beginning or somewhere in the middle of the
        chunk."""

        chunks = dict()
        last_chunk = None
        addresses = set(addresses)

        # get all first chunk offsets (from all arenas/heapinfo structs;
        # MMAPPED chunks can be ignored). The first chunk of a memory region
        # has the prev_inuse bit set, but no previous chunk.
        first_chunk_offsets = set()
        for arena in self.arenas:
            if arena.is_main_arena:
                first_chunk_offsets.add(arena.first_chunk.v())

            for heapinfo in arena.heaps:
                first_chunk_offsets.add(heapinfo.first_chunk.v())


        addresses_to_remove = set()

        # get_all_chunks returns allocated chunks first, and then freed ones
        # so it doesn't screw up the 'last_chunk' functionality
        for chunk in self.get_all_chunks():

            # we already found hits for those, so we don't check for them
            # anymore
            if addresses_to_remove:
                addresses ^= addresses_to_remove
                addresses_to_remove = set()

            for address in addresses:
                if chunk.v() <= address < chunk.v() + chunk.chunksize():
                    addresses_to_remove.add(address)

                    chunk_to_add = None

                    if not ignore_prevsize and \
                            chunk.v() not in first_chunk_offsets and \
                            chunk.v() <= address \
                            < chunk.v() + chunk.get_prev_size().obj_size and \
                            chunk.prev_inuse():
                        # hit is in prev_size field and PREV_INUSE is set, so
                        # the last chunk is in use and hence the prev_size
                        # field belongs to him
                        # Note: MMAPPED chunks don't use next chunk's
                        # prev_size field, as it is not guaranteed that an
                        # MMAPPED chunk has a following MMAPPED chunk. As,
                        # however, MMAPPED chunks don't use the PREV_INUSE bit,
                        # we don't have to explicitly test for MMAPPED chunks
                        # to exclude them

                        if chunk.is_freed_chunk():
                            # in this case, the 'last_chunk' method doesn't
                            # work, as get_all_chunks does not walk the memory
                            # chunk by chunk, but first returns all allocated
                            # chunks from memory and then all freed chunks
                            # gathered via the bins. So as a ugly workaround
                            # (that could be improved in the future), we walk
                            # the allocated chunks until we find this freed
                            # chunk's previous chunk
                            found_previous_chunk = False
                            for allocated_chunk in \
                                    self.get_all_allocated_chunks():

                                if allocated_chunk.v() + \
                                        allocated_chunk.chunksize() \
                                        == chunk.v():
                                    last_chunk = allocated_chunk
                                    found_previous_chunk = True

                            if not found_previous_chunk:
                                self.session.logging.warn(
                                    "We didn't find a previous chunk for "
                                    "a freed chunk. This is unexpected and "
                                    "will lead to wrong results")


                        if last_chunk:
                            if last_chunk.v() + last_chunk.chunksize() \
                                    == chunk.v():
                                chunk_to_add = last_chunk

                            else:
                                self.session.logging.warn(
                                    "The current previous chunk for chunk at "
                                    "offset 0x{:x} does not seem to be its "
                                    "predecessor. This is unexpected at this "
                                    "point and might indicate a major "
                                    "problem.")

                        else:
                            self.session.logging.error(
                                "Error: last_chunk shouldn't be None at "
                                "this point.")

                            if self.session.GetParameter("debug"):
                                pdb.post_mortem()


                    else:
                        chunk_to_add = chunk


                    if chunk_to_add not in list(chunks.keys()):
                        # in the case, multiple addresses match the same chunk:
                        chunks[chunk_to_add] = set()

                    chunks[chunk_to_add].add(address)

            last_chunk = chunk

        return chunks



    # Note: Does not return chunks containing pointers to the prev_size field
    # of the first chunk of the main heap/ heap_info area; but this shouldn't
    # be the case anyways. For all other chunks, the prev_size field is treated
    # appropriately
    def search_chunks_for_needle(self, search_string=None, search_regex=None,
                                 pointers=None, search_struct=False):
        """Searches all chunks for the given pointer(s) and returns the ones
        containing them. It only searches the data part of a chunk (e.g.
        not fd/bk fields for bin chunks).

        pointers = a list of int pointers
        search_string/search_regex = a string or regex to search for in a chunk
        search_struct = if set to True, also fields like size and fd/bk for bin
        chunks are included
        """

        # as searching every chunk for data is inefficient, we first search all
        # vmas and correlate the hits with known chunks afterwards
        if pointers:
            hits = self.search_vmas_for_needle(pointers=pointers,
                                               vmas=self.heap_vmas)

        elif search_string:
            hits = self.search_vmas_for_needle(search_string=search_string,
                                               vmas=self.heap_vmas)

        elif search_regex:
            hits = self.search_vmas_for_needle(search_regex=search_regex,
                                               vmas=self.heap_vmas)

        else:
            return None

        result = dict()

        # the result structure is:
        # { chunk_with_hit: {
        #   { needle (string or pointer): { offsets: {offsets} }, needle2: ...}
        # } , more chunks }:

        for chunk in self.get_all_chunks():

            start, length = chunk.start_and_length()
            end = start + length

            if search_struct:
                start = chunk.size.obj_offset

            for hit in hits:
                if start <= hit['hit'] < end:
                    if chunk not in list(result.keys()):
                        result[chunk] = {hit['needle']: {hit['hit']}}

                    else:
                        if hit['needle'] not in list(result[chunk].keys()):
                            result[chunk][hit['needle']] = {hit['hit']}

                        else:
                            result[chunk][hit['needle']].add(hit['hit'])

        return result



    def _ebp_unrolling(self, ebp, vma):
        """Helper function for carving hidden MMAPPED chunks.
        Tries to follow EBP pointers to the first one and returns its offset.
        """

        if not vma.vm_start <= ebp < vma.vm_end:
            return None

        temp = ebp
        last_ebp = 0

        # infinite loop protection, when dealing with arbitrary data instead of
        # real ebp pointers
        max_steps = 0x2000
        i = 0

        while vma.vm_start <= temp < vma.vm_end and last_ebp != temp and \
                i < max_steps:
            last_ebp = temp
            temp = (self.process_as.read(temp, self._size_sz))
            temp = struct.unpack('I' if self._size_sz == 4 else 'Q', temp)[0]
            i += 1

        return last_ebp


    def _search_first_hidden_mmapped_chunk(self, initial_address, vma):
        """Helper function for carving hidden MMAPPED chunks.
        This function searches from initial_address until vma.vm_end for a
        MMAPPED chunk and returns it if found."""

        # As mmapped regions are normally on pagesize boundaries (4096 or a
        # multiple of it) we only look at those offsets for a mmapped chunk
        offset = self._get_page_aligned_address(initial_address)

        # as the minimum size for mmapped chunks is normally equal to pagesize
        # (4096 bytes), there should be at least 4096 bytes behind the current
        # position - see also comment in check_and_report_mmap_chunk
        distance = vma.vm_end - offset

        while distance >= self._min_pagesize:

            temp_chunk = self.profile.malloc_chunk(offset=offset,
                                                   vm=self.process_as)

            if temp_chunk.get_prev_size() == 0 and \
                    temp_chunk.chunksize() >= self._min_pagesize and \
                    temp_chunk.chunksize() % self._min_pagesize == 0 and \
                    temp_chunk.is_mmapped() and \
                    not temp_chunk.prev_inuse() and \
                    not temp_chunk.non_main_arena() and \
                    temp_chunk.v() + temp_chunk.chunksize() <= vma.vm_end:

                return temp_chunk

            else:
                offset += self._min_pagesize
                distance = vma.vm_end - offset


    def calculate_statistics(self):
        """Sets the class attribute self.statistics with a dict containing
        e.g. number of allocated/freed/fastbin chunks, their sizes..."""

        if not self.get_main_arena():
            return

        number_of_arenas = len(self.arenas)
        number_of_heaps = 0

        number_of_bin_chunks = 0
        size_of_bin_chunks = 0
        number_of_fastbin_chunks = 0
        size_of_fastbin_chunks = 0
        number_of_top_chunks = 0
        size_of_top_chunks = 0

        number_of_main_chunks = 0
        size_of_main_chunks = 0

        # both bottom chunks are excluded here
        number_of_thread_chunks = 0
        size_of_thread_chunks = 0

        number_of_bottom_chunks = 0
        size_of_bottom_chunks = 0

        ##### mallinfo specific values ####
        # includes bin and top chunks, also for empty main arena
        mallinfo_number_of_free_chunks = 0

        # the sum of the system_mem fields from all arenas
        non_mmapped_bytes = 0

        # total_allocated_space is the sum of all allocated chunk sizes
        # _except_ mmapped chunks
        # includes also heap/arena struct sizes and bottom chunks
        total_allocated_space = 0

        # includes top chunk and fastbins
        total_free_space = 0
        ####################################


        for arena in self.arenas:

            non_mmapped_bytes += arena.system_mem

            if arena.top_chunk:
                number_of_top_chunks += 1
                size_of_top_chunks += arena.top_chunk.chunksize()

            # mallinfo always counts the top chunk for the main arena, even if
            # the main heap and hence the top chunk doesn't exist (in these
            # cases, the top chunk pointer points to the top member of the
            # malloc_state struct: to itself)
            elif arena.is_main_arena:
                mallinfo_number_of_free_chunks += 1

            for chunk in arena.freed_fast_chunks:
                number_of_fastbin_chunks += 1
                size_of_fastbin_chunks += chunk.chunksize()

            for chunk in arena.freed_chunks:
                number_of_bin_chunks += 1
                size_of_bin_chunks += chunk.chunksize()

            if arena.is_main_arena:
                for chunk in self._allocated_chunks_for_main_arena():
                    number_of_main_chunks += 1
                    size_of_main_chunks += chunk.chunksize()

            else:
                for chunk in self._allocated_chunks_for_thread_arena(arena):

                    # The last bottom chunk has a size of 0 but in fact takes
                    # 2 * size_sz. As it normally isn't returned by
                    # allocated_chunks_for_thread_arena, and has a chunksize
                    # of 0, we manually add it's size
                    if chunk.is_bottom_chunk:
                        number_of_bottom_chunks += 2
                        size_of_bottom_chunks += chunk.chunksize()
                        size_of_bottom_chunks += self._size_sz * 2

                    else:
                        size_of_thread_chunks += chunk.chunksize()
                        number_of_thread_chunks += 1


                # total_allocated_space includes also the allocated space from
                # heap_info and malloc_state structs (except for the
                # main_arena)
                for heap in arena.heaps:
                    number_of_heaps += 1
                    total_allocated_space += heap.first_chunk.v() - heap.v()


        ### mallinfo specific calculation
        total_free_space += size_of_top_chunks
        total_free_space += size_of_fastbin_chunks
        total_free_space += size_of_bin_chunks

        mallinfo_number_of_free_chunks += number_of_bin_chunks
        mallinfo_number_of_free_chunks += number_of_top_chunks

        total_allocated_space += size_of_main_chunks
        total_allocated_space += size_of_thread_chunks
        total_allocated_space += size_of_bottom_chunks
        ######################

        statistics = dict()
        statistics['number_of_arenas'] = number_of_arenas
        statistics['number_of_heaps'] = number_of_heaps
        statistics['number_of_bin_chunks'] = number_of_bin_chunks
        statistics['size_of_bin_chunks'] = size_of_bin_chunks
        statistics['number_of_fastbin_chunks'] = number_of_fastbin_chunks
        statistics['size_of_fastbin_chunks'] = size_of_fastbin_chunks
        statistics['number_of_top_chunks'] = number_of_top_chunks
        statistics['size_of_top_chunks'] = size_of_top_chunks
        statistics['number_of_main_chunks'] = number_of_main_chunks
        statistics['size_of_main_chunks'] = size_of_main_chunks
        statistics['number_of_thread_chunks'] = number_of_thread_chunks
        statistics['size_of_thread_chunks'] = size_of_thread_chunks
        statistics['number_of_bottom_chunks'] = number_of_bottom_chunks
        statistics['size_of_bottom_chunks'] = size_of_bottom_chunks

        statistics['non_mmapped_bytes'] = non_mmapped_bytes
        statistics['total_allocated_space'] = total_allocated_space
        statistics['total_free_space'] = total_free_space
        statistics['mallinfo_number_of_free_chunks'] = \
            mallinfo_number_of_free_chunks

        self.statistics = statistics

        self._calculate_mmapped_statistics()


    def _calculate_mmapped_statistics(self):
        """Calculates number and size of MMAPPED chunks and sets those values
        for the statistics attribute. Is outsourced from calculate_statistics
        to be able to recalculate MMAPPED chunks statistics when hidden MMAPPED
        chunks have been found, without having to recalculate all statistics.
        """

        # This function shouldn't normally be called without having previously
        # called calculate_statistics
        if not self.statistics:
            return

        number_of_mmapped_chunks = 0
        size_of_mmapped_chunks = 0

        for chunk in self.get_all_mmapped_chunks():
            number_of_mmapped_chunks += 1
            size_of_mmapped_chunks += chunk.chunksize()

        self.statistics['number_of_mmapped_chunks'] = number_of_mmapped_chunks
        self.statistics['size_of_mmapped_chunks'] = size_of_mmapped_chunks


    def _compare_vma_sizes_with_chunks(self):
        """This function calculates the size of all relevant vm_areas and
        compares the result with the size of all allocated and freed chunks.
        It returns True if both values are the same.
        """

        if not self.get_main_arena():
            return None

        vma_sum = 0
        for vma in self.heap_vmas:
            vma_sum += (vma['vma'].vm_end - vma['vma'].vm_start)

        if not self.statistics:
            self.calculate_statistics()

        chunk_sum = (self.statistics['total_allocated_space']
                     + self.statistics['total_free_space']
                     + self.statistics['size_of_mmapped_chunks'])

        chunk_sum += sum(self._mmap_slack_space.values())
        chunk_sum += sum(self._heap_slack_space.values())


        vma_sum += sum([x.chunksize() for x in self._hidden_chunks])
        # as we can't simply add the vm_area for the hidden chunks to the
        # vma_sum, as it contains also other data, we add the hidden chunks
        # and their slack space to the vma_sum
        #
        # _mmap_slack_space is filled with the "chunk" after the last mmapped
        # chunk which isn't really a chunk but only empty space. to get them,
        # we call next_chunk on any mmapped chunk, including the last one for
        # each memory segment (which is the relevant one)
        hidden_next_chunks = [x.next_chunk() for x in self._hidden_chunks]

        # now we get only the slack space for the hidden chunks
        vma_sum += sum([y for x, y in self._mmap_slack_space.items()
                        if x in hidden_next_chunks])

        vma_sum -= self._first_chunk_distance

        return chunk_sum == vma_sum


    def check_and_report_size_inconsistencies(self):
        """Calls size comparison methods to verify the gathered chunks and
        prints warnings on any discrepancies."""

        if not self.statistics:
            self.calculate_statistics()

        if self.compare_mmapped_chunks_with_mp_() is False:
            self.session.logging.info(
                "The values from the malloc_par struct don't correspond to "
                "our found MMAPPED chunks. This indicates we didn't find all "
                "MMAPPED chunks and that they probably hide somewhere in a "
                "vm_area. So we now try to carve them.")

            self._carve_register_mmapped_chunks_hidden_behind_stack()
            self._calculate_mmapped_statistics()

            if self.compare_mmapped_chunks_with_mp_() is False:
                self.session.logging.info(
                    "Seems like we didn't find (all) MMAPPED chunks behind "
                    "stack frames. We now search in all anonymous vm_areas "
                    "for them, which might however lead to false positives.")

                self._carve_and_register_hidden_mmapped_chunks_globally()
                self._calculate_mmapped_statistics()

                if self.compare_mmapped_chunks_with_mp_() is False:
                    self.session.logging.warn(
                        "The calculated count and size of all MMAPPED chunks "
                        "doesn't meet the values from the gathered malloc_par "
                        "struct. We found {:d} MMAPPED chunks with a total "
                        "size of {:d} and the malloc_par struct reports {:d} "
                        "MMAPPED chunks with a total size of {:d}. This "
                        "either results from an error in getting all chunks "
                        "or in choosing the correct vm_areas. Either way, the "
                        "MMAPPED results will be wrong."
                        .format(self.statistics['number_of_mmapped_chunks'],
                                self.statistics['size_of_mmapped_chunks'],
                                self.mp_.n_mmaps,
                                self.mp_.mmapped_mem))


                    self._search_stacks_for_mmap_pointers()

                else:
                    self.session.logging.info(
                        "Seems like all missing MMAPPED chunks have been "
                        "found.")

            else:
                self.session.logging.info(
                    "Seems like all missing MMAPPED chunks have been found.")



        if self._compare_vma_sizes_with_chunks() is False:
            for warning in self._mmapped_warnings:
                self.session.logging.warn(warning)

            self.session.logging.warn(
                "The calculated sum from all heap objects and chunks does not "
                "meet the sum from all heap relevant vm_areas. This either "
                "results from an error in getting all chunks or in choosing "
                "the relevant vm_areas. Either way, the results are most "
                "probably unreliable.")

        self._compare_and_report_system_mem_sizes()


    def _compare_and_report_system_mem_sizes(self):
        """Compares the identified vmas for main and thread heaps with their
        system_mem values and prints warnings on any discrepancies."""

        main_heap_size = 0
        size_all_vmas = 0
        mmapped_first_chunk_pointers = \
            [x.v() for x in self.get_main_arena().mmapped_first_chunks]

        relevant_vmas = [x for x in self.heap_vmas
                         if x['vma'].vm_start
                         not in mmapped_first_chunk_pointers]

        for vma in relevant_vmas:
            size = (vma['vma'].vm_end - vma['vma'].vm_start)

            if vma['name'] == self._main_heap_identifier:
                # as the main heap can spread among multiple vm_areas, we add
                # their sizes up
                main_heap_size += size

            size_all_vmas += size

        if not self._has_dummy_arena:
            main_heap_size -= self._first_chunk_distance

        if self.get_main_arena().system_mem != main_heap_size:
            self.session.logging.warn(
                "The size of the vm_area identified to belong to the main "
                "arena does not have the same size as the system_mem value of "
                "that arena. This shouldn't be the case and might indicate, "
                "that the wrong vm_area has been selected and hence leading "
                "to wrong chunks output.")

        else:
            system_mem_size = 0
            for arena in self.arenas:
                system_mem_size += arena.system_mem

            system_mem_size += sum(self._heap_slack_space.values())

            if not self._has_dummy_arena:
                system_mem_size += self._first_chunk_distance

            if size_all_vmas != system_mem_size:
                self.session.logging.warn(
                    "The size of at least one arena (its system_mem value) "
                    "does not have the same size as the corresponding "
                    "vm_areas. This shouldn't be the case and might indicate, "
                    "that either some vm_areas are missing or that at least "
                    "one vm_area has been mistakenly chosen. This leads "
                    "either to missing or wrong chunks in the output.")


    def _initialize_malloc_par(self):
        """Initializes the malloc_par struct."""

        mp_offset = None

        if self.mp_offset:
            mp_offset = self.mp_offset

        else:
            self.mp_offset = self.profile.get_constant('mp_')
            mp_offset = self.mp_offset

        if mp_offset:
            if self._libc_offset:
                mp_offset += self._libc_offset

            self.mp_ = self.profile.malloc_par(offset=mp_offset,
                                               vm=self.process_as)

            self._check_and_report_mp_for_being_swapped(self.mp_)

        else:
            self.session.logging.warn(
                "It seems like the debug information for the mp_ offset are "
                "missing. This means some checks/verifications can't be done.")


    def compare_mmapped_chunks_with_mp_(self):
        """Compares the calculated count and size of all MMAPPED chunks with
        the data from the malloc_par struct.
        Returns None on any errors, True if count and sizes match and
        otherwise False."""

        if not self.get_main_arena() or not self.mp_:
            return None

        if not self.statistics:
            self.calculate_statistics()

        if self.mp_.mmapped_mem == self.statistics['size_of_mmapped_chunks'] \
                and self.mp_.n_mmaps \
                == self.statistics['number_of_mmapped_chunks']:
            return True

        return False



    def get_mallinfo_string(self):
        """Returns statistics according to the mallinfo struct except for
        keepcost and usmblks.
        See http://man7.org/linux/man-pages/man3/mallinfo.3.html
        """

        if not self.get_main_arena():
            return None

        if not self.statistics:
            self.calculate_statistics()

        result = ""

        result += ("Total non-mmapped bytes (arena):       "
                   + str(self.statistics['non_mmapped_bytes'])
                   + "\n")
        result += ("# of free chunks (ordblks):            "
                   + str(self.statistics['mallinfo_number_of_free_chunks'])
                   + "\n")
        result += ("# of free fastbin blocks (smblks):     "
                   + str(self.statistics['number_of_fastbin_chunks'])
                   + "\n")
        result += ("# of mapped regions (hblks):           "
                   + str(self.statistics['number_of_mmapped_chunks'])
                   + "\n")
        result += ("Bytes in mapped regions (hblkhd):      "
                   + str(self.statistics['size_of_mmapped_chunks'])
                   + "\n")
        result += ("Free bytes held in fastbins (fsmblks): "
                   + str(self.statistics['size_of_fastbin_chunks'])
                   + "\n")
        result += ("Total allocated space (uordblks):      "
                   + str(self.statistics['total_allocated_space'])
                   + "\n")
        result += ("Total free space (fordblks):           "
                   + str(self.statistics['total_free_space'])
                   + "\n")

        return result


    @classmethod
    def is_active(cls, session):
        return session.profile.metadata("os") == 'linux'


    __args = [
        dict(name='main_arena', type='IntParser', default=None,
             help=("The main_arena pointer either extracted from the "
                   "statically linked ELF binary or from the libc library.")),
        dict(name='malloc_par', type='IntParser', default=None,
             help=("The malloc_par pointer either extracted from the "
                   "linked ELF binary or from the libc library."))
    ]


class HeapOverview(HeapAnalysis):
    """Tries to gather a list of all arenas/heaps and all allocated chunks."""

    __name = "heapinfo"

    table_header = [
        dict(name="pid", width=6),
        dict(name="arenas", width=6),
        dict(name="heap_infos", width=10),
        dict(name="non_mmapped_chunks", width=20),
        dict(name="non_mmapped_chunks_size", width=26),
        dict(name="mmapped_chunks", width=16),
        dict(name="mmapped_chunks_size", width=22),
        dict(name="freed_chunks", width=14),
        dict(name="freed_chunks_size", width=20)
    ]

    def collect(self):

        for task in self.filter_processes():
            if not task.mm:
                self.session.logging.warn("Analysis for Task {:d} aborted as "
                                          "it seems to be a kernel thread.\n"
                                          .format(task.pid))
                continue

            try:
                if self.init_for_task(task):

                    freed_chunks = self.statistics['number_of_bin_chunks']
                    freed_chunks += self.statistics['number_of_fastbin_chunks']
                    freed_size = self.statistics['size_of_bin_chunks']
                    freed_size += self.statistics['size_of_fastbin_chunks']

                    non_mmapped_chunks = \
                        self.statistics['number_of_main_chunks']
                    non_mmapped_chunks += \
                        self.statistics['number_of_thread_chunks']
                    non_mmapped_size = self.statistics['size_of_main_chunks']
                    non_mmapped_size += \
                        self.statistics['size_of_thread_chunks']


                    yield(task.pid,
                          self.statistics['number_of_arenas'],
                          self.statistics['number_of_heaps'],
                          non_mmapped_chunks,
                          non_mmapped_size,
                          self.statistics['number_of_mmapped_chunks'],
                          self.statistics['size_of_mmapped_chunks'],
                          freed_chunks,
                          freed_size)

            except:
                self.session.logging.warn("Analysis for Task {:d} failed.\n"
                                          .format(task.pid))
                self.session.logging.warn(traceback.format_exc())



class HeapObjects(HeapAnalysis):
    """Prints the structs of heap objects (such as allocated chunks, arenas,
    ...)"""

    __name = "heapobjects"

    __args = [
        dict(name='print_allocated', type="Boolean", default=False,
             help="prints all allocated chunk structs"),
        dict(name='print_freed', type="Boolean", default=False,
             help="prints all freed chunk structs"),
        dict(name='print_mmapped', type="Boolean", default=False,
             help="prints all MMAPPED chunk structs"),
        dict(name='print_mallinfo', type="Boolean", default=False,
             help="prints statistic information, similar to glibc's mallinfo")
    ]



    def render(self, renderer):

        for task in self.filter_processes():
            if not task.mm:
                self.session.logging.warn("Object dumping aborted for Task "
                                          "{:d} as it seems to be a kernel "
                                          "thread.\n".format(task.pid))
                continue

            try:
                if self.init_for_task(task):
                    # as printing requires walking allocated chunks, we prevent
                    # walking the memory two times


                    print_output_separator = '=' * 65
                    format_string = "{0:s} {1:s} {0:s}"

                    renderer.write("\n")
                    renderer.write(
                        format_string
                        .format('=' * 18, 'Arena and heap_info objects'))
                    renderer.write("\n")

                    for arena in self.arenas:
                        if arena.is_main_arena:
                            renderer.write("Main_arena: ")
                            renderer.write(arena)
                            renderer.write("\n")

                            renderer.write("First chunk: ")
                            renderer.write(arena.first_chunk)
                            renderer.write("\n")

                        else:
                            renderer.write("Thread arena: ")
                            renderer.write(arena)
                            renderer.write("\n")


                        renderer.write("Top chunk: ")
                        renderer.write(arena.top_chunk)
                        renderer.write("\n")


                        for heap in arena.heaps:
                            renderer.write(heap)
                            renderer.write("\n")
                            renderer.write("First chunk: ")
                            renderer.write(heap.first_chunk)
                            renderer.write("\n")

                    renderer.write(print_output_separator)
                    renderer.write("\n")


                    if self.plugin_args.print_allocated:
                        renderer.write("\n")
                        renderer.write(
                            format_string
                            .format('=' * 18, 'Allocated Main Arena Chunks'))

                        renderer.write("\n")
                        for chunk in self.get_all_allocated_main_chunks():
                            renderer.write(chunk)
                            renderer.write("\n")

                        renderer.write(print_output_separator)
                        renderer.write("\n")

                        renderer.write("\n")
                        renderer.write(
                            format_string
                            .format('=' * 18, 'Allocated Thread Arena Chunks'))

                        renderer.write("\n")
                        for chunk in self.get_all_allocated_thread_chunks():
                            renderer.write(chunk)
                            renderer.write("\n")

                        renderer.write(print_output_separator)
                        renderer.write("\n")

                    if self.plugin_args.print_mmapped or \
                            self.plugin_args.print_allocated:
                        renderer.write("\n")
                        renderer.write(
                            format_string
                            .format('=' * 18, 'MMAPPED Chunks'))

                        renderer.write("\n")
                        for chunk in self.get_all_mmapped_chunks():
                            renderer.write(chunk)
                            renderer.write("\n")

                        renderer.write(print_output_separator)
                        renderer.write("\n")


                    if self.plugin_args.print_freed:
                        renderer.write("\n")
                        renderer.write(
                            format_string
                            .format('=' * 18, 'Freed Chunks'))

                        renderer.write("\n")
                        for chunk in self.get_all_freed_chunks():
                            renderer.write(chunk)
                            renderer.write("\n")

                        renderer.write(print_output_separator)
                        renderer.write("\n")


                    if self.plugin_args.print_mallinfo:
                        renderer.write("\n")
                        renderer.write(
                            format_string
                            .format('=' * 18, 'Mallinfo Output'))

                        renderer.write("\n")
                        renderer.write(self.get_mallinfo_string())
                        renderer.write("\n")
                        renderer.write(print_output_separator)
                        renderer.write("\n")

            except:
                self.session.logging.warn(
                    "Object dumping for Task {:d} failed.\n".format(task.pid))
                self.session.logging.warn(traceback.format_exc())


class HeapChunkDumper(core.DirectoryDumperMixin, HeapAnalysis):
    """Dumps allocated/freed chunks from selected processes """

    __name = "heapdump"
    _filename_format_string = ("{:d}.{}-chunk_offset-0x{:0{:d}X}_size-{:d}"
                               "_dumped-{:d}_stripped-{:d}.dmp")


    table_header = [
        dict(name="pid", width=6),
        dict(name="allocated", width=12),
        dict(name="freed_bin", width=12),
        dict(name="freed_fastbin", width=14),
        dict(name="top_chunks", width=12)
    ]


    def collect(self):

        for task in self.filter_processes():
            if not task.mm:
                continue

            if self.init_for_task(task):

                allocated_chunk_count = 0
                freed_fastbin_chunks = 0
                freed_bin_chunks = 0
                top_chunks = 0

                for arena in self.arenas:
                    if arena.top_chunk:
                        top_chunks += 1
                        self.dump_chunk_to_file(arena.top_chunk,
                                                arena.top_chunk.chunksize(),
                                                'top')

                    if arena.is_main_arena:
                        for chunk in self.get_all_allocated_chunks_for_arena(
                                arena):
                            allocated_chunk_count += 1
                            self.dump_chunk_to_file(chunk,
                                                    chunk.chunksize(),
                                                    'allocated-main')

                    else:
                        for chunk in self.get_all_allocated_chunks_for_arena(
                                arena):
                            allocated_chunk_count += 1
                            chunksize = chunk.chunksize()
                            identifier = 'allocated-thread'

                            if chunk.is_bottom_chunk:
                                chunksize -= self._size_sz
                                identifier = 'bottom'

                            self.dump_chunk_to_file(chunk,
                                                    chunksize,
                                                    identifier)

                for chunk in self.get_all_mmapped_chunks():
                    allocated_chunk_count += 1


                    self.dump_chunk_to_file(chunk,
                                            chunk.chunksize(),
                                            'allocated-mmapped')


                # here we differentiate fastbin chunks from bin chunks, as
                # fastbin chunks only overwrite one dword size of data with a
                # pointer while bin chunks overwrite 2
                for freed_chunk in self.get_all_freed_fastbin_chunks():
                    freed_fastbin_chunks += 1
                    self.dump_chunk_to_file(freed_chunk,
                                            freed_chunk.chunksize(),
                                            'freed-fastbin')

                for freed_chunk in self.get_all_freed_bin_chunks():
                    freed_bin_chunks += 1
                    self.dump_chunk_to_file(freed_chunk,
                                            freed_chunk.chunksize(),
                                            'freed-bin')


                yield dict(pid=task.pid, allocated=allocated_chunk_count,
                           freed_bin=freed_bin_chunks,
                           freed_fastbin=freed_fastbin_chunks,
                           top_chunks=top_chunks)


    def dump_chunk_to_file(self, chunk, chunksize, identifier):
        """Used as the wrapper to dump a given chunk to file."""

        fd_offset = self.profile.get_obj_offset("malloc_chunk", "fd")

        try:
            data = chunk.to_string()
            start, _ = chunk.start_and_length()

            filename = self._filename_format_string.format(
                self.task.pid, identifier, chunk.v(), self._size_sz * 2,
                chunksize, len(data), start - chunk.v() - fd_offset)

            output_file = open(self.dump_dir + os.sep + filename, 'wb')
            output_file.write(data)

        except:
            print(traceback.format_exc())

        finally:
            try:
                output_file.close()

            except:
                pass


# TODO: yara support
class HeapPointerSearch(HeapAnalysis):
    """Searches all chunks for the given string, regex or pointer(s)."""

    __name = "heapsearch"

    def render(self, renderer):
        if not (self.plugin_args.pointers or self.plugin_args.string
                or self.plugin_args.regex or self.plugin_args.chunk_addresses):
            renderer.write("Specify something to search for.\n")

        else:
            for task in self.filter_processes():
                if not task.mm:
                    continue

                if self.init_for_task(task):

                    hits = dict()

                    if self.plugin_args.pointers:
                        hits = self.search_chunks_for_needle(
                            pointers=self.plugin_args.pointers,
                            search_struct=self.plugin_args.search_struct)


                    if self.plugin_args.string:
                        temp_hits = self.search_chunks_for_needle(
                            search_string=self.plugin_args.string,
                            search_struct=self.plugin_args.search_struct)

                        for chunk, needles in temp_hits.items():
                            if chunk in list(hits.keys()):
                                hits[chunk].update(needles)

                            else:
                                hits[chunk] = needles


                    if self.plugin_args.regex:
                        temp_hits = self.search_chunks_for_needle(
                            search_regex=self.plugin_args.regex,
                            search_struct=self.plugin_args.search_struct)

                        for chunk, needles in temp_hits.items():
                            if chunk in list(hits.keys()):
                                hits[chunk].update(needles)

                            else:
                                hits[chunk] = needles



                    if self.plugin_args.chunk_addresses:
                        # first we gather the chunks of interest
                        base_chunks = self.get_chunks_for_addresses(
                            self.plugin_args.chunk_addresses,
                            ignore_prevsize=True)

                        if base_chunks:
                            renderer.write(
                                "\n\nWe found the following chunks for the "
                                "given chunk_addresses (all other chunks will "
                                "now be searched for pointers to them): \n\n")

                        else:
                            renderer.write(
                                "\n\nWe did not find any chunks for the given "
                                "chunk_addresses.\n\n")

                        for base_chunk in base_chunks:
                            renderer.write(base_chunk)
                            renderer.write("\n\n")

                            start = base_chunk.v()
                            if base_chunk.prev_inuse():
                                start += base_chunk.get_prev_size().obj_size

                            pointers = list(range(
                                start, sum(base_chunk.start_and_length())))

                            # now, we search in all chunks for pointers to the
                            # chunks of interest
                            temp_hits = self.search_chunks_for_needle(
                                pointers=pointers,
                                search_struct=self.plugin_args.search_struct)


                            # temp_hits: chunks that contain a pointer to one
                            # of the chunks of interest and the pointer values
                            for hit_chunk, data in temp_hits.items():
                                if hit_chunk not in list(hits.keys()):
                                    hits[hit_chunk] = {base_chunk: data}

                                elif base_chunk not in list(hits[hit_chunk].keys()):
                                    hits[hit_chunk][base_chunk] = data

                                else:
                                    # Chunks as dict keys are not type safe.
                                    # E.g. if a chunk with the same base offset
                                    # as pointer exists as a key, a test such
                                    # as 'pointer in hits[hit_chunk].keys()'
                                    # will return True.
                                    # Hence, we use the following workaround
                                    # to update hits
                                    try:
                                        hits[hit_chunk][base_chunk].update(
                                            data)
                                    except KeyError:
                                        hits[hit_chunk][base_chunk] = data

                    if hits:
                        renderer.write("{0:s} Search results {0:s}"
                                       .format('=' * 18))

                    for chunk, needles in hits.items():
                        renderer.write("\n\n")
                        renderer.write("The chunk (0x{:X}) below contains:\n\n"
                                       .format(chunk.v()))

                        for needle, data in needles.items():
                            if isinstance(needle, malloc_chunk):
                                renderer.write(
                                    "The following pointers at the given "
                                    "offset(s) to the chunk at offset "
                                    "0x{:X}:\n".format(needle.v()))

                                renderer.write("Pointer    Offset(s)\n")
                                renderer.write("----------------------\n")
                                for pointer, offsets in data.items():
                                    renderer.write(
                                        "0x{:X}: ".format(pointer)
                                        + ', '.join(["0x{:X}".format(x) for x
                                                     in offsets]))

                                    renderer.write("\n")

                                renderer.write("\n")

                            else:
                                renderer.write(
                                    "The following needle at the given "
                                    "offset(s):\n")
                                renderer.write("Needle     Offset(s)\n")
                                renderer.write("----------------------\n")

                                if len(data) <= 9:
                                    renderer.write(
                                        (hex(needle) if isinstance(needle, int)
                                         else needle) + ": "
                                        + ', '.join(["0x{:X}".format(x) for x
                                                     in data]))

                                    renderer.write("\n")
                                else:
                                    renderer.write(
                                        (hex(needle) if isinstance(needle, int)
                                         else needle) + ": The needle has been"
                                        + "found on {0:d} offsets.\n"
                                        .format(len(data)))

                                renderer.write("\n")

                        renderer.write("\n\n")
                        renderer.write(chunk)
                        renderer.write("\n\n{:s}\n".format('=' * 60))


    __args = [
        dict(name='pointers', type='ArrayIntParser', default=None,
             help=("Prints chunks that contain exactly the given pointer(s). "
                   "The pointer(s) can be given as (hexa)decimal numbers.")),
        dict(name='chunk_addresses', type='ArrayIntParser', default=None,
             help=("Expects address(es) belonging to a chunk(s) of interest, "
                   "and prints all chunks having a pointer somewhere into "
                   "the data part of that chunk(s).")),
        dict(name='regex', type='str', default=None,
             help=("Searches all chunks with the given regex and prints "
                   "all hits.")),
        dict(name='string', type='str', default=None,
             help=("Searches all chunks for the given string and prints "
                   "all hits.")),
        dict(name='search_struct', type="Boolean", default=False,
             help=("Includes the malloc_struct fields in the search process "
                   "such as size and fd/bk for bin chunks (but not its own "
                   "prev_size field). This is normally not desired and hence "
                   "deactivated by default."))
    ]



class HeapReferenceSearch(HeapAnalysis):
    """Examines the data part of the given chunk for references to other
    chunks."""

    __name = "heaprefs"

    def CreateAllocationMap(self, start, length):
        """Creates colorful hex map for pointers in a chunk"""

        address_map = core.AddressMap()

        if self.session.profile.metadata("arch") == 'I386':
            int_string = 'I'

        else:
            int_string = 'Q'

        offset_and_pointers = dict()


        # walks the chunk of interest and gathers all potential chunk pointers
        # with their offset within the chunk
        for i in range(start, start+length, 4):
            temp = struct.unpack(int_string,
                                 self.process_as.read(i, self._size_sz))[0]
            if temp == 0:
                continue

            elif temp in list(offset_and_pointers.keys()):
                offset_and_pointers[temp].append(i)

            else:
                offset_and_pointers[temp] = [i]


        # gathers a list of chunks, referenced by the potential chunk pointers
        chunks = self.get_chunks_for_addresses(list(offset_and_pointers.keys()))

        for chunk, pointers in chunks.items():
            for pointer_offset, offsets in offset_and_pointers.items():
                for pointer in pointers:
                    if pointer == pointer_offset:
                        for offset in offsets:
                            address_map.AddRange(
                                offset,
                                offset+self._size_sz,
                                'Pointer to chunk at offset: 0x{:X}'
                                .format(chunk.v()),
                                color_index=self._get_next_color_index(
                                    chunk.v()))

        return address_map


    def _get_next_color_index(self, pointer):
        """Returns color index values that are easy to read on command line."""

        if pointer not in list(self._color_index_dict.keys()):
            self._current_color_index += 1

            while self._current_color_index in self._color_index_blacklist:
                self._current_color_index += 1

            self._color_index_dict[pointer] = self._current_color_index

            return self._current_color_index

        return self._color_index_dict[pointer]


    def render(self, renderer):

        for task in self.filter_processes():
            if not task.mm:
                continue

            if self.init_for_task(task):

                # first we gather the chunk for the given pointer
                chunks = self.get_chunks_for_addresses(
                    self.plugin_args.chunk_addresses, ignore_prevsize=True)

                for chunk, pointers in chunks.items():
                    renderer.write("\n\n")
                    renderer.write(
                        "Examining chunk at offset 0x{:X}, belonging to the "
                        "given address(es): {:s}".format(
                            chunk.v(), ', '.join([hex(x) for x in pointers])))

                    renderer.write("\n\n")
                    start, length = chunk.start_and_length()

                    if length % 4:
                        self.session.logging.warn(
                            "The chunk at offset 0x{:x} seems to have "
                            "a length not divisable by 4. This is unexpected "
                            "and indicates a fundamental error.".format(chunk.v()))

                    dump = self.session.plugins.dump(
                        offset=start, length=length,
                        address_map=self.CreateAllocationMap(start, length),
                        address_space=self.process_as)

                    dump.render(renderer)


    __args = [
        dict(name='chunk_addresses', type='ArrayIntParser', default=None,
             help=("The address(es) belonging to chunks of interest. Those "
                   "chunks are then examined for references to other chunks."))
    ]


    def __init__(self, **kwargs):
        super(HeapReferenceSearch, self).__init__(**kwargs)
        self._current_color_index = 0
        self._color_index_dict = dict()
        self._color_index_blacklist = [10, 16, 18, 19, 22, 24, 25, 34]



class malloc_chunk(obj.Struct):
    """Extends the malloc_chunk class"""

    def __init__(self, **kwargs):
        super(malloc_chunk, self).__init__(**kwargs)
        self._prev_size = None
        self._prev_inuse = None
        self._non_main_arena = None
        self._is_mmapped = None
        self._size = None
        self._chunksize = None
        self.is_bottom_chunk = False
        self.is_fastbin_chunk = False
        self.is_bin_chunk = False
        self.is_top_chunk = False

        # since glibc 2.25, size and prev_size have been renamed
        if hasattr(self, 'mchunk_size'):
            self.size = self.mchunk_size
            self._size = self.size

        if hasattr(self, 'mchunk_prev_size'):
            self.prev_size = self.mchunk_prev_size
            self._prev_size = self.prev_size


    ###################### Performance related functions ######################
    ### Performance related function
    ### As retrieving data from the memory like the size field is pretty time
    ### consuming, these functions prevent repeated retrieval of values
    ### These functions save especially time when dealing with thousands of
    ### chunks.
    ###########################################################################
    def get_size(self):
        """Returns the value of the size field, including potential bit flags.
        """

        if not self._size:
            self._size = self.size

        return self._size


    def get_prev_size(self):
        """Returns the value of the prev_size field."""

        if not self._prev_size:
            self._prev_size = self.prev_size

        return self._prev_size


    ###########################################################################

############ Taken from glibc-2.23/malloc/malloc.c #####################

    def prev_inuse(self):
        """Returns True if this chunk has its PREV_INUSE bit set."""

        if not self._prev_inuse:
            self._prev_inuse = (self.get_size() & _PREV_INUSE) == _PREV_INUSE

        return self._prev_inuse


    def is_mmapped(self):
        """Returns True if the chunk has been obtained with mmap()."""

        if not self._is_mmapped:
            self._is_mmapped = (self.get_size() & _IS_MMAPPED) == _IS_MMAPPED

        return self._is_mmapped


    def non_main_arena(self):
        """Returns True if current chunk does NOT belong to main_arena."""

        if not self._non_main_arena:
            self._non_main_arena = (self.get_size() & _NON_MAIN_ARENA) \
                                   == _NON_MAIN_ARENA

        return self._non_main_arena


    def chunksize(self):
        """Returns the real size of a chunk, excluding bit flags."""

        # as it got called often, this little improvement can save several
        # seconds on a chunk count >100.000
        if not self._chunksize:
            self._chunksize = self.get_size() & ~ _SIZE_BITS

        return self._chunksize


###############################################################################

    def is_allocated_chunk(self):
        """Returns True if this chunk is not a bottom, small/large bin, fastbin
        or top chunk."""

        return not self.is_fastbin_chunk and not self.is_bin_chunk and \
            not self.is_top_chunk and not self.is_bottom_chunk


    def is_freed_chunk(self):
        """Returns True if this chunk is a small/large bin, fastbin or top
        chunk."""

        return self.is_fastbin_chunk or self.is_bin_chunk or self.is_top_chunk


    # TODO make this function easier
    def start_and_length(self):
        """Returns the relevant start offset and length for dumping."""

        # For allocated chunks, the fd pointer doesn't contain a pointer
        # but is the beginning of data
        data_offset = self.fd.obj_offset

        # we are not testing whether or not the given length is in range of
        # the current chunk
        length = self.chunksize()

        if self.is_bin_chunk:
            if self.chunksize() >= _MIN_LARGE_SIZE:
                data_offset = self.v() + self.obj_size

            else:
                data_offset = self.fd_nextsize.obj_offset

            # The data part of an allocated chunk reaches until the next
            # chunk's prev_size field. On freeing the current chunk, the next
            # chunk's prev_size field is overwritten with the size information
            # from this chunk and hence doesn't anymore contain useful data
            length -= self.prev_size.obj_size

        elif self.is_fastbin_chunk:
            data_offset = self.bk.obj_offset

        elif self.is_bottom_chunk:
            # bottom chunk specific subtraction to get the only part which
            # contains useful data
            length -= self.bk.obj_size

            # this subtraction just compensates the addition done later on
            length -= self.prev_size.obj_size

        elif self.is_top_chunk or self.is_mmapped():
            # both chunks don't use the prev_size field of the next chunk
            length -= self.prev_size.obj_size


        # we first subtract the offset to the beginning of data
        length -= data_offset - self.v()

        # and now add the size of the prev_size field, as the data part of any
        # chunk except bins, top, mmapped and bottom chunks reaches until the
        # next chunk's prev_size field
        length += self.prev_size.obj_size

        return [data_offset, length]



    def to_string(self, length=None, offset=None):
        """Returns a string containing the data part of the given allocated
        chunk.
        The length parameter is intended only for printing shorter
        parts of the current chunk.
        The offset makes only sense in combination with the length parameter
        and starts from the beginning of the chunk, so an offset of 4 on a
        32 bit architecture starts on the size member."""

        data_offset = None
        size = None

        if not length or not offset:
            start, leng = self.start_and_length()
            data_offset = self.v() + offset if offset else start
            size = length if length else leng


        if size <= 0:
            return ""

        data = self.obj_vm.read(data_offset, size)

        if not data:
            return obj.NoneObject("Unable to read {0} bytes from {1}",
                                  size, data_offset)

        return data


    def next_chunk(self):
        """Returns the following chunk."""

        return self.obj_profile.malloc_chunk(self.v() + self.chunksize(),
                                             vm=self.obj_vm)

    def is_in_use(self):
        """Returns true, if this chunk is in use: the next chunk's PREV_INUSE
        flag is set."""

        return self.next_chunk().prev_inuse()


    def next_chunk_generator(self):
        """Returns all following chunks, beginning with the current."""

        yield self

        next_chunk = self.next_chunk()

        # We expect the last chunk to have null size field.
        # Further circumstances must be checked in calling functions.
        while next_chunk.get_size() != 0:
            yield next_chunk
            next_chunk = self.obj_profile.malloc_chunk(
                next_chunk.v() + next_chunk.chunksize(), vm=self.obj_vm)

        # TODO at the moment we return the last chunk, as other functions
        # rely on it for tests; this should be changed in a future release
        yield next_chunk



class _heap_info(obj.Struct):
    """Extends the heap_info class"""

    def __init__(self, **kwargs):
        super(_heap_info, self).__init__(**kwargs)
        self.first_chunk = None


class malloc_state(obj.Struct):
    """Extends the malloc_state class"""

    def __init__(self, **kwargs):
        super(malloc_state, self).__init__(**kwargs)

        # only used on non main_arenas
        self.heaps = list()

        self.is_main_arena = False

        # only used with main_arena
        self.first_chunk = None

        # only used with main_arena
        self.mmapped_first_chunks = None

        # result from walking the fastbinsY lists
        self.freed_fast_chunks = list(self.get_freed_chunks_fastbins())

        # result from walking the bins lists
        self.freed_chunks = list(self.get_freed_chunks_bins())

        # We generally use this variable instead of the struct field.
        # The reason is the scenario, in which we didn't find any main_arena
        # and need to set up a dummy arena
        self.top_chunk = None
        if self.top:
            self.top_chunk = self.top.dereference()
            self.top_chunk.is_top_chunk = True

        # both allocated chunks lists are used for performance improvements
        # on analysis systems with few memory resources the usage of these
        # lists is discouraged as some processes can have >400000 chunks which
        # e.g. results in 1.6 GB memory usage
        self.allocated_chunks = None

        # only used with main_arena
        self.allocated_mmapped_chunks = None


    def get_freed_chunks_fastbins(self):
        """Returns all freed chunks referenced by the fastbins."""

        for fastbin in self.fastbinsY:
            for fast_chunk in fastbin.walk_list("fd"):
                fast_chunk.is_fastbin_chunk = True
                yield fast_chunk


    def get_freed_chunks_bins(self):
        """Returns all freed chunks referenced by the bins."""

        # as every second pointer is the backwards pointer to the same list of
        # freed chunks, we simply skip ahead this second pointer
        for _bin in self.bins[::2]:
            for free_chunk in _bin.walk_list("fd"):
                if not self.v() <= free_chunk.v() \
                        < (self.v() + self.struct_size):

                    free_chunk.is_bin_chunk = True
                    yield free_chunk



class GlibcProfile32(basic.Profile32Bits, basic.BasicClasses):
    """Profile to parse basic Glibc structures."""

    __abstract = True

    # types come from Glibc's malloc/malloc.c
    # for simplicity, we just don't use the new chunk size/prev_size names
    # coming with 2.25
    glibc_base_vtype_32 = {
        "malloc_chunk": [24, {
            "bk": [12, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "bk_nextsize": [20, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "fd": [8, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "fd_nextsize": [16, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "prev_size": [0, ["unsigned int"]],
            "size": [4, ["unsigned int"]]
        }],
        "_heap_info": [16, {
            "ar_ptr": [0, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "mprotect_size": [12, ["unsigned int"]],
            "pad": [16, ["Array", {
                "count": 0,
                "target": "char",
                "target_args": None
            }]],
            "prev": [4, ["Pointer", {
                "target": "_heap_info",
                "target_args": None
            }]],
            "size": [8, ["unsigned int"]]
        }]
    }

    mp_224_vtype_32 = {
        "malloc_par": [48, {
            "arena_max": [16, ["unsigned int"]],
            "arena_test": [12, ["unsigned int"]],
            "max_mmapped_mem": [40, ["unsigned int"]],
            "max_n_mmaps": [28, ["int"]],
            "mmap_threshold": [8, ["unsigned int"]],
            "mmapped_mem": [36, ["unsigned int"]],
            "n_mmaps": [20, ["int"]],
            "n_mmaps_max": [24, ["int"]],
            "no_dyn_threshold": [32, ["int"]],
            "sbrk_base": [44, ["Pointer", {
                "target": "char",
                "target_args": None
            }]],
            "top_pad": [4, ["unsigned int"]],
            "trim_threshold": [0, ["long unsigned int"]]
        }]
    }

    mp_220_vtype_32 = {
        "malloc_par": [52, {
            "arena_max": [16, ["unsigned int"]],
            "arena_test": [12, ["unsigned int"]],
            "max_mmapped_mem": [40, ["unsigned int"]],
            "max_n_mmaps": [28, ["int"]],
            "max_total_mem": [44, ["unsigned int"]],
            "mmap_threshold": [8, ["unsigned int"]],
            "mmapped_mem": [36, ["unsigned int"]],
            "n_mmaps": [20, ["int"]],
            "n_mmaps_max": [24, ["int"]],
            "no_dyn_threshold": [32, ["int"]],
            "sbrk_base": [48, ["Pointer", {
                "target": "char",
                "target_args": None
            }]],
            "top_pad": [4, ["unsigned int"]],
            "trim_threshold": [0, ["long unsigned int"]]
        }]
    }

    ms_223_vtype_32 = {
        "malloc_state": [1108, {
            "attached_threads": [1096, ["unsigned int"]],
            "binmap": [1072, ["Array", {
                "count": 4,
                "target": "unsigned int",
                "target_args": None
            }]],
            "bins": [56, ["Array", {
                "count": 254,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "fastbinsY": [8, ["Array", {
                "count": 10,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "flags": [4, ["int"]],
            "last_remainder": [52, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "max_system_mem": [1104, ["unsigned int"]],
            "mutex": [0, ["int"]],
            "next": [1088, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "next_free": [1092, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "system_mem": [1100, ["unsigned int"]],
            "top": [48, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]]
        }]
    }


    ms_220_vtype_32 = {
        "malloc_state": [1104, {
            "binmap": [1072, ["Array", {
                "count": 4,
                "target": "unsigned int",
                "target_args": None
            }]],
            "bins": [56, ["Array", {
                "count": 254,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "fastbinsY": [8, ["Array", {
                "count": 10,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "flags": [4, ["int"]],
            "last_remainder": [52, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "max_system_mem": [1100, ["unsigned int"]],
            "mutex": [0, ["int"]],
            "next": [1088, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "next_free": [1092, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "system_mem": [1096, ["unsigned int"]],
            "top": [48, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]]
        }]
    }

    version_dict = {
        '220': [glibc_base_vtype_32, ms_220_vtype_32, mp_220_vtype_32],
        '223': [glibc_base_vtype_32, ms_223_vtype_32, mp_220_vtype_32],
        '224': [glibc_base_vtype_32, ms_223_vtype_32, mp_224_vtype_32]
    }


    def __init__(self, version=None, **kwargs):
        super(GlibcProfile32, self).__init__(**kwargs)
        profile = dict()

        # at the moment: either 2.24 (2.25 is similar to 2.24), 2.23 or < 2.23
        if version:
            try:
                self.session.logging.info(
                    "We are using I386 glibc profile version {:s}"
                    .format(version))

                for vtypes in self.version_dict[version]:
                    profile.update(vtypes)

            except KeyError:
                self.session.logging.warn(
                    "The given version string: {:s} is not in our dict. "
                    "This is unexpected.".format(version))

        if not profile:
            # the default profile to use
            self.session.logging.info(
                "We are using the I386 default glibc profile version 2.24")

            for vtypes in self.version_dict['224']:
                profile.update(vtypes)

        self.add_types(profile)


class GlibcProfile64(basic.ProfileLP64, basic.BasicClasses):
    """Profile to parse basic Glibc structures."""

    __abstract = True

    # types come from Glibc's malloc/malloc.c
    glibc_base_vtype_64 = {
        "malloc_chunk": [48, {
            "bk": [24, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "bk_nextsize": [40, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "fd": [16, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "fd_nextsize": [32, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "prev_size": [0, ["long unsigned int"]],
            "size": [8, ["long unsigned int"]]
        }],
        "_heap_info": [32, {
            "ar_ptr": [0, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "mprotect_size": [24, ["long unsigned int"]],
            "pad": [32, ["Array", {
                "count": 0,
                "target": "char",
                "target_args": None
            }]],
            "prev": [8, ["Pointer", {
                "target": "_heap_info",
                "target_args": None
            }]],
            "size": [16, ["long unsigned int"]]
        }]
    }

    mp_220_vtype_64 = {
        "malloc_par": [88, {
            "arena_max": [32, ["long unsigned int"]],
            "arena_test": [24, ["long unsigned int"]],
            "max_mmapped_mem": [64, ["long unsigned int"]],
            "max_n_mmaps": [48, ["int"]],
            "max_total_mem": [72, ["long unsigned int"]],
            "mmap_threshold": [16, ["long unsigned int"]],
            "mmapped_mem": [56, ["long unsigned int"]],
            "n_mmaps": [40, ["int"]],
            "n_mmaps_max": [44, ["int"]],
            "no_dyn_threshold": [52, ["int"]],
            "sbrk_base": [80, ["Pointer", {
                "target": "char",
                "target_args": None
            }]],
            "top_pad": [8, ["long unsigned int"]],
            "trim_threshold": [0, ["long unsigned int"]]
        }]
    }

    mp_224_vtype_64 = {
        "malloc_par": [80, {
            "arena_max": [32, ["long unsigned int"]],
            "arena_test": [24, ["long unsigned int"]],
            "max_mmapped_mem": [64, ["long unsigned int"]],
            "max_n_mmaps": [48, ["int"]],
            "mmap_threshold": [16, ["long unsigned int"]],
            "mmapped_mem": [56, ["long unsigned int"]],
            "n_mmaps": [40, ["int"]],
            "n_mmaps_max": [44, ["int"]],
            "no_dyn_threshold": [52, ["int"]],
            "sbrk_base": [72, ["Pointer", {
                "target": "char",
                "target_args": None
            }]],
            "top_pad": [8, ["long unsigned int"]],
            "trim_threshold": [0, ["long unsigned int"]]
        }]
    }

    ms_223_vtype_64 = {
        "malloc_state": [2192, {
            "mutex": [0, ["int"]],
            "flags": [4, ["int"]],
            "fastbinsY": [8, ["Array", {
                "count": 10,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "top": [88, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "last_remainder": [96, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "bins": [104, ["Array", {
                "count": 254,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "binmap": [2136, ["Array", {
                "count": 4,
                "target": "unsigned int",
                "target_args": None
            }]],
            "next": [2152, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "next_free": [2160, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "attached_threads": [2168, ["long unsigned int"]],
            "system_mem": [2176, ["long unsigned int"]],
            "max_system_mem": [2184, ["long unsigned int"]]
        }]
    }

    ms_220_vtype_64 = {
        "malloc_state": [2184, {
            "mutex": [0, ["int"]],
            "flags": [4, ["int"]],
            "fastbinsY": [8, ["Array", {
                "count": 10,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "top": [88, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "last_remainder": [96, ["Pointer", {
                "target": "malloc_chunk",
                "target_args": None
            }]],
            "bins": [104, ["Array", {
                "count": 254,
                "target": "Pointer",
                "target_args": {
                    "target": "malloc_chunk",
                    "target_args": None
                }
            }]],
            "binmap": [2136, ["Array", {
                "count": 4,
                "target": "unsigned int",
                "target_args": None
            }]],
            "next": [2152, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "next_free": [2160, ["Pointer", {
                "target": "malloc_state",
                "target_args": None
            }]],
            "system_mem": [2168, ["long unsigned int"]],
            "max_system_mem": [2176, ["long unsigned int"]]
        }]
    }

    version_dict = {
        '220': [glibc_base_vtype_64, ms_220_vtype_64, mp_220_vtype_64],
        '223': [glibc_base_vtype_64, ms_223_vtype_64, mp_220_vtype_64],
        '224': [glibc_base_vtype_64, ms_223_vtype_64, mp_224_vtype_64]
    }


    def __init__(self, version=None, **kwargs):
        super(GlibcProfile64, self).__init__(**kwargs)
        profile = self.glibc_base_vtype_64

        # at the moment: either 2.24 (2.25 is similar to 2.24), 2.23 or < 2.23
        if version:
            try:
                self.session.logging.info(
                    "We are using AMD64 glibc profile version {:s}"
                    .format(version))

                for vtypes in self.version_dict[version]:
                    profile.update(vtypes)

            except KeyError:
                self.session.logging.warn(
                    "The given version string: {:s} is not in our dict. "
                    "This is unexpected.".format(version))

        if not profile:
            # the default profile to use
            self.session.logging.info(
                "We are using the AMD64 default glibc profile version 2.24")

            for vtypes in self.version_dict['224']:
                profile.update(vtypes)

        self.add_types(profile)
