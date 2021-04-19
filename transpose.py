import os
import pretty_midi
import music21

keys = {
    "A": 0,
    "A#": 1,
    "B": 2,
    "C": 3,
    "C#": 4,
    "D": 5,
    "D#": 6,
    "E": 7,
    "F": 8,
    "F#": 9,
    "G": 10,
    "G#": 11
}

inverted_keys = {v: k for k, v in keys.items()}


def find_key(midi_path):
    score = music21.converter.parse(midi_path)
    key = score.analyze('key')
    return key.tonic.name.replace('-', ''), key.mode


def get_number_of_steps_for_transposition_to(midi_path, target_key):
    key, mode = find_key(midi_path)
    key_nr = keys[key]
    target_key_nr = keys[target_key]

    if mode == 'minor':
        target_key_nr -= 3

    above_key_target_nr = key_nr
    if above_key_target_nr < key_nr:
        above_key_target_nr += 12

    below_key_target_nr = key_nr
    if below_key_target_nr > key_nr:
        below_key_target_nr -= 12

    transpose_steps_down = key_nr - below_key_target_nr
    transpose_steps_up = above_key_target_nr - key_nr
    if transpose_steps_up > transpose_steps_down:
        transpose_steps = -transpose_steps_down
    else:
        transpose_steps = transpose_steps_up

    return transpose_steps
