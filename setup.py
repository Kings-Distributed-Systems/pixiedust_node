from setuptools import setup, find_packages
import subprocess
import os
from setuptools.command.build_py import build_py


setup(name='pixiedust_node',
      version='0.2.6',
      description='Pixiedust extension for Node.js',
      url='https://github.com/pixiedust/pixiedust_node',
      install_requires=['pixiedust', 'pandas', 'ipython', 'numpy'],
      package_data={
        '': ['*.js','*.json']
      },
      author='David Taieb, Glynn Bird',
      author_email='david_taieb@us.ibm.com, glynn.bird@gmail.com',
      license='Apache 2.0',
      packages=find_packages(),
      include_package_data=False,
      zip_safe=False)
