"""
This script performs various operations on binary files, including encryption,
key generation, and analysis of executable sections.
"""

import angr
import os
import shutil
import get_exe_fields
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import hashlib
import hmac
import sys

iv = b'\xC2\x40\xEC\xD0\x63\x63\x62\xDF\xBF\xD3\xB8\xF2\x7C\x3B\x80\x02'
hash_function = hashlib.sha256
PC_ID_LENGTH = 32
AES_KEY_LENGTH = 16
IV_SIZE = 16

"""
@brief Prints a byte object in hexadecimal format with space separation.

@param byte_obj: The byte object to be printed.
"""
def print_hex_format(byte_obj):
    hex_string = byte_obj.hex()
    formatted_hex = ' '.join(hex_string[i:i+2] for i in range(0, len(hex_string), 2))
    print(formatted_hex)


"""
@brief Computes the HMAC digest of the given key and data.

@param key: The key for HMAC computation.
@param data: The data to be digested.
@return: The computed HMAC digest.
"""
def hmac_digest(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hash_function).digest()


"""
@brief Performs the extract step of HKDF.

@param salt: The salt value (a non-secret random value).
@param ikm: The input keying material.
@return: The pseudorandom key (PRK).
"""
def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    if len(salt) == 0:
        salt = bytes([0] * hash_function().digest_size)
    return hmac_digest(salt, ikm)


"""
@brief Performs the expand step of HKDF.

@param prk: The pseudorandom key.
@param info: The context and application specific information.
@param length: The length of the output keying material in bytes.
@return: The output keying material (OKM).
"""
def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    t = b""
    okm = b""
    i = 0
    while len(okm) < length:
        i += 1
        t = hmac_digest(prk, t + info + bytes([i]))
        okm += t
    return okm[:length]


"""
@brief Implements the HKDF (HMAC-based Key Derivation Function) algorithm.

@param salt: The salt value (a non-secret random value).
@param ikm: The input keying material.
@param info: The context and application specific information.
@param length: The length of the output keying material in bytes.
@return: The output keying material (OKM).
"""
def hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hkdf_extract(salt, ikm)
    return hkdf_expand(prk, info, length)


"""
@brief Retrieves the addresses of absolute relocations in a binary file.

@param binary_path: Path to the binary file.
@return: A sorted list of relocation addresses.
"""
def get_relocation_addresses(binary_path):
    project = angr.Project(binary_path, auto_load_libs=False)
    main_object = project.loader.main_object
    image_base = main_object.min_addr
    relocation_addresses = set()
    for reloc in main_object.relocs:
        if reloc.symbol is None:
            relocation_addresses.add(reloc.rebased_addr - image_base)
    return sorted(list(relocation_addresses))


"""
@brief Filters out blocks that contain exactly the expected number of relocations.

@param ranges: List of address ranges to filter.
@param relocation_addresses: List of relocation addresses.
@return: Filtered list of address ranges.
"""
def filter_blocks_by_relocations(ranges, relocation_addresses):
    filtered_ranges = []
    for start, end in ranges:
        block_size = end - start
        expected_relocs = block_size // 8  # Changed to 8 for 64-bit
        relocs_in_block = sum(1 for addr in relocation_addresses if start <= addr < end)
        if relocs_in_block < expected_relocs:
            filtered_ranges.append((start, end))
    return filtered_ranges


"""
@brief Retrieves the address ranges of basic blocks in a binary file.

@param binary_path: Path to the binary file.
@param limit_factor: Minimum size for a block to be considered.
@return: List of tuples representing address ranges of basic blocks.
"""
def get_basic_block_ranges(binary_path, limit_factor):
    project = angr.Project(binary_path, auto_load_libs=False)
    image_base = project.loader.main_object.min_addr
    cfg = project.analyses.CFGFast()
    address_ranges = []
    jump_targets = set()
    for node in cfg.nodes():
        for successor in cfg.get_successors(node):
            jump_targets.add(successor.addr)
    for node in cfg.nodes():
        block_start = node.addr
        block_end = node.addr + node.size
        for target in jump_targets:
            if block_start < target < block_end:
                if target - block_start >= limit_factor:
                    address_ranges.append((block_start, target))
                block_start = target
        if block_end - block_start >= limit_factor:
            address_ranges.append((block_start, block_end))
    address_ranges = [(start - image_base, end - image_base) for start, end in address_ranges]
    address_ranges.sort(key=lambda x: x[0])
    return address_ranges


"""
@brief Encrypts data using AES-CTR mode.

@param data: The data to be encrypted.
@param aes_key: The AES key for encryption.
@return: The encrypted data.
"""
def encrypt_data(data, aes_key):
    backend = default_backend()
    cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv), backend=backend)
    encryptor = cipher.encryptor()
    encrypted_data = encryptor.update(data) + encryptor.finalize()
    return encrypted_data


"""
@brief Generates a key using HKDF based on the given address and file content.

@param address: The address to use in key generation.
@param file_name: The name of the file containing necessary data.
@return: The generated key.
"""
def generate_key(address, file_name):
    with open(file_name, 'rb') as file:
        pc_id = file.read(PC_ID_LENGTH)
        key_bytes = file.read(AES_KEY_LENGTH)
        print_hex_format(key_bytes)
        print_hex_format(hkdf(address.to_bytes(8, byteorder='little'), key_bytes, pc_id, AES_KEY_LENGTH))
        return hkdf(address.to_bytes(8, byteorder='little'), key_bytes, pc_id, AES_KEY_LENGTH)


"""
@brief Encrypts specified blocks in a file and creates a new output file.

@param file_name: Name of the input file.
@param blocks: List of block ranges to encrypt.
@param out_dir: Output directory for the encrypted file.
@return: Name of the output file.
"""
def enc_blocks(file_name, blocks, out_dir):
    out_file_name = os.path.join(out_dir, os.path.basename(file_name) + "_out.exe")
    shutil.copyfile(file_name, out_file_name)
    project = angr.Project(file_name, auto_load_libs=False)
    raw_factor = int(get_exe_fields.get_text_section_virtual_address(file_name), 16) - get_exe_fields.get_text_section_addresses(file_name)[0]
    blocks = sorted(blocks)
    with open(out_file_name, "r+b") as out_file, open(file_name, "rb") as read_file:
        current_position = 0
        for (start_block, end_block) in blocks:
            start_raw = start_block - raw_factor
            print(f"raw address {hex(start_raw)}")
            end_raw = end_block - raw_factor
            if start_raw > current_position:
                read_length = start_raw - current_position
                out_file.write(read_file.read(read_length))
                current_position = start_raw
            read_file.seek(start_raw, 0)
            block_to_encrypt = read_file.read(end_raw - start_raw)
            cur_key = generate_key(start_block, os.path.join(out_dir, "License.dat"))
            encrypted_block = encrypt_data(block_to_encrypt, cur_key)
            out_file.seek(start_raw, 0)
            out_file.write(encrypted_block)
            current_position = end_raw
        remaining_size = os.path.getsize(file_name) - current_position
        if remaining_size > 0:
            out_file.write(read_file.read(remaining_size))
    return out_file_name


"""
@brief Writes the list of block ranges to a binary file.

@param blocks: List of block ranges to write.
@param out_dir: Output directory for the block list file.
"""
def write_blocks_file(blocks, out_dir):
    block_file_name = os.path.join(out_dir, "blocks_list.bin")
    with open(block_file_name, 'wb') as block_file:
        for start_address, end_address in blocks:
            start_bytes = start_address.to_bytes(8, byteorder='little', signed=False)
            end_bytes = end_address.to_bytes(8, byteorder='little', signed=False)
            block_file.write(start_bytes)
            block_file.write(end_bytes)


"""
@brief Finds dynamic jumps and calls in a 64-bit executable.

@param exe_path: Path to the executable file.
@return: List of addresses of dynamic instructions.
"""
def find_dynamic_jumps_calls_64bit(exe_path):
    project = angr.Project(exe_path, auto_load_libs=False)
    cfg = project.analyses.CFGFast()
    image_base = project.loader.main_object.min_addr
    executable_sections = [sec for sec in project.loader.main_object.sections if sec.is_executable]
    if not executable_sections:
        raise ValueError("No executable sections found in the binary")
    text_start = min(sec.min_addr for sec in executable_sections)
    text_end = max(sec.max_addr for sec in executable_sections)
    dynamic_instructions = []
    for addr, function in cfg.functions.items():
        for block in function.blocks:
            for instruction in block.capstone.insns:
                if instruction.insn.mnemonic.startswith('j') or instruction.insn.mnemonic == 'call':
                    op_str = instruction.insn.op_str
                    if any(reg in op_str for reg in ['rax', 'rbx', 'rcx', 'rdx', 'rsi', 'rdi', 'rsp', 'rbp', 'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15']):
                        dynamic_instructions.append(instruction.address - image_base)
                    elif op_str.startswith('qword ptr'):
                        try:
                            target_addr = int(op_str.split('[')[-1].split(']')[0], 16)
                            if text_start <= target_addr <= text_end:
                                dynamic_instructions.append(instruction.address - image_base)
                        except ValueError:
                            dynamic_instructions.append(instruction.address - image_base)
    return dynamic_instructions


"""
@brief Writes the list of call addresses to a binary file.

@param addresses: List of call addresses to write.
@param out_dir: Output directory for the call address list file.
"""
def write_call_address_file(addresses, out_dir):
    block_file_name = os.path.join(out_dir, "call_address_list.bin")
    with open(block_file_name, 'wb') as block_file:
        for address in addresses:
            tmp = address.to_bytes(8, byteorder='little', signed=False)
            block_file.write(tmp)


"""
Main execution block of the script.
Performs various operations including block encryption, 
writing block information, and analyzing dynamic jumps and calls.
"""
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <path_to_exe>")
        sys.exit(1)

    binary_path = sys.argv[1]
    dir_path = os.path.dirname(binary_path)
    out_dir = os.path.join(dir_path, "out")
    os.makedirs(out_dir, exist_ok=True)

    license_path = os.path.join(out_dir, "License.dat")
    if not os.path.exists(license_path):
        print(f"Warning: License.dat not found in {out_dir}")
        print("Please ensure License.dat is in the 'out' directory.")
        sys.exit(1)

    limit_factor = 1
    reallocation_table = get_relocation_addresses(binary_path)
    for addr in reallocation_table:
        print(addr)
    ranges = get_basic_block_ranges(binary_path, limit_factor)
    ranges = filter_blocks_by_relocations(ranges, reallocation_table)
    ranges = sorted(ranges)
    enc_blocks(binary_path, ranges, out_dir)
    write_blocks_file(ranges, out_dir)
    dynamic_jumps = find_dynamic_jumps_calls_64bit(binary_path)
    write_call_address_file(dynamic_jumps, out_dir)

    print("Basic Block Address Ranges (start, end) [virtual addresses]:")
    for start, end in ranges:
        print(f"0x{start:x} - 0x{end:x}")
    print(len(ranges))